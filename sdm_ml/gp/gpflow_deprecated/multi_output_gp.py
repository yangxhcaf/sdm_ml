import os
import pickle
import numpy as np
import pandas as pd
import gpflow as gpf
import gpflow.multioutput.features as mf
import gpflow.multioutput.kernels as mk
from tqdm import tqdm
from os.path import join
from sklearn.model_selection import KFold
from sklearn.preprocessing import StandardScaler

from .utils import (find_starting_z, save_gpflow_model,
                    log_probability_via_sampling)
from sdm_ml.presence_absence_model import PresenceAbsenceModel
from ml_tools.utils import load_pickle_safely
from .utils import (calculate_log_joint_bernoulli_likelihood,
                    load_saved_gpflow_model, compute_latent_predictions)
from .mean_functions import MultiOutputMeanFunction
from ml_tools.evaluation import neg_log_loss_with_labels, multi_class_eval


class MultiOutputGP(PresenceAbsenceModel):

    def __init__(self, n_inducing, n_latent, kernel, maxiter=int(1E6),
                 train_inducing_points=True, seed=2, whiten=True,
                 verbose_fit=True, n_draws_predict=int(1E4),
                 mean_function=lambda: None):

        np.random.seed(seed)

        self.is_fit = False

        self.n_inducing = n_inducing
        self.n_latent = n_latent
        self.kernel = kernel
        self.maxiter = maxiter
        self.train_inducing_points = train_inducing_points
        self.seed = seed
        self.whiten = whiten
        self.verbose_fit = verbose_fit
        self.m = None
        self.n_draws_predict = n_draws_predict
        self.mean_function = mean_function

    def fit(self, X, y):

        self.scaler = StandardScaler()

        X = self.scaler.fit_transform(X)
        M = self.n_inducing
        L = self.n_latent

        self.Z = find_starting_z(X, self.n_inducing, use_minibatching=False)

        q_mu = np.zeros((M, L))
        q_sqrt = np.repeat(np.eye(M)[None, ...], L, axis=0) * 1.0

        feature = mf.MixedKernelSharedMof(gpf.features.InducingPoints(self.Z))

        self.m = gpf.models.SVGP(X, y, self.kernel,
                                 gpf.likelihoods.Bernoulli(), feat=feature,
                                 q_mu=q_mu, q_sqrt=q_sqrt, whiten=self.whiten,
                                 mean_function=self.mean_function())

        self.m.feature.set_trainable(self.train_inducing_points)

        opt = gpf.train.ScipyOptimizer(options={'maxfun': self.maxiter})
        opt.minimize(self.m, disp=self.verbose_fit, maxiter=self.maxiter)
        self.is_fit = True

    def calculate_log_likelihood(self, X, y):

        # TODO: Consider enforcing some shapes

        assert self.is_fit

        X = self.scaler.transform(X)

        if self.verbose_fit:
            print('Calculating mean and covariance...')

        means, covs = self.get_f_mean_and_cov(X)

        if self.verbose_fit:
            print('Done.')

        log_liks = np.zeros(means.shape[0])

        if self.verbose_fit:

            print('Estimating log likelihood.')
            y_it = tqdm(y)

        else:

            y_it = y

        for i, (cur_mean, cur_cov, cur_y) in enumerate(
                zip(means, covs, y_it)):

            draws = np.random.multivariate_normal(
                cur_mean, cur_cov, size=self.n_draws_predict)
            log_liks[i] = calculate_log_joint_bernoulli_likelihood(
                draws, cur_y)

        return log_liks

    def predict_log_marginal_probabilities(self, X):

        # For the margins, we should be able to do the same thing as the
        # single-output GP.
        # Predict mean and variances for all species.
        # I expect these to be (n_sites x n_species).
        X = self.scaler.transform(X)

        means, vars = self.m.predict_f(X)

        all_log_probs = list()

        for cur_means, cur_vars in zip(means.T, vars.T):

            cur_log_probs = log_probability_via_sampling(
                cur_means, np.sqrt(cur_vars), self.n_draws_predict)

            all_log_probs.append(cur_log_probs)

        all_log_probs = np.stack(all_log_probs, axis=1)

        return all_log_probs

    def save_model(self, target_folder):

        os.makedirs(target_folder, exist_ok=True)

        assert self.is_fit, "Model must be fit before saving!"

        # Also store the model with the old-style pickle for large models
        with open(join(target_folder, 'parameters.pkl'), 'wb') as f:
            pickle.dump(self.m.read_trainables(), f)

        # Try to use the new-style saving technique if possible
        try:
            target = join(target_folder, 'saved_model')
            save_gpflow_model(self.m, target, exist_ok=True)
        except Exception:
            print("Failed to save model using new GPFlow style.")

        table_result = self.m.as_pandas_table()
        table_result.to_pickle(join(target_folder, 'model_results.pkl'))

        # Also store: scaler, Z.
        data_pickle = {
            'Z': self.Z,
            'scaler': self.scaler
        }

        with open(join(target_folder, 'data.pkl'), 'wb') as f:
            pickle.dump(data_pickle, f)

    @staticmethod
    def build_default_mean_function(n_outputs):

        return MultiOutputMeanFunction(n_outputs)

    @staticmethod
    def build_default_kernel(n_dims, n_kernels, n_outputs, add_bias=False,
                             w_prior=0.1, kern_var_trainable=False,
                             rbf_var=0.1, bias_var=0.1):

        L = n_kernels
        D = n_dims
        P = n_outputs

        with gpf.defer_build():

            # Use some sensible defaults
            kern_list = [
                gpf.kernels.RBF(D, ARD=True, variance=1.0) for _ in
                range(L)]

            for cur_kern in kern_list:
                cur_kern.lengthscales.prior = gpf.priors.Gamma(3, 3)
                cur_kern.variance = rbf_var
                cur_kern.variance.set_trainable(kern_var_trainable)

            if add_bias:
                kern_list[-1] = gpf.kernels.Bias(D)
                kern_list[-1].variance = bias_var
                kern_list[-1].variance.set_trainable(kern_var_trainable)

            W_init = np.random.randn(P, L)
            kernel = mk.SeparateMixedMok(kern_list, W_init)

            kernel.W.prior = gpf.priors.Gaussian(0, w_prior)

        return kernel

    @classmethod
    def restore_from_file_new_style(cls, saved_model, data_pickle):

        m = load_saved_gpflow_model(saved_model)
        Z = m.feature.feat.Z.value
        n_latent = len(m.kern.kernels)
        whiten = m.whiten
        trainable = m.feature.feat.Z.trainable
        n_inducing = Z.shape[1]

        mogp = cls(n_inducing, n_latent, m.kern,
                   train_inducing_points=trainable, whiten=whiten)

        mogp.m = m
        mogp.is_fit = True
        mogp.Z = Z

        data = load_pickle_safely(data_pickle)
        scaler = data['scaler']

        mogp.scaler = scaler

        return mogp

    def get_f_mean_and_cov(self, X):

        assert self.is_fit
        mu, cov = compute_latent_predictions(self.m, X, mix_latents=True)

        if not self.m.mean_function.empty:
            c = self.m.mean_function.c.value
            mu += c

        return mu, cov

    @staticmethod
    def cross_val_score(X, y, model_creation_fun, save_dir, n_folds=4):

        kfold = KFold(n_splits=n_folds)
        fold_liks = np.empty(n_folds)

        for i, (cur_train_ind, cur_test_ind) in tqdm(
                enumerate(kfold.split(X, y))):

            cur_X = X[cur_train_ind]
            cur_y = y[cur_train_ind]

            gpf.reset_default_graph_and_session()

            model = model_creation_fun()

            model.fit(cur_X, cur_y)

            cur_save_dir = join(save_dir, f'fold_{i + 1}')
            os.makedirs(cur_save_dir, exist_ok=True)

            model.save_model(cur_save_dir)

            cur_test_x = X[cur_test_ind]
            cur_test_y = y[cur_test_ind]

            log_liks = model.calculate_log_likelihood(cur_test_x, cur_test_y)
            marg_pred = pd.DataFrame(
                model.predict_marginal_probabilities(cur_test_x))

            marg_pred.to_csv(join(cur_save_dir, 'marginal_probs.csv'))
            pd.DataFrame(cur_test_y).to_csv(join(cur_save_dir, 'y_t.csv'))

            # I am also interested in the log loss.
            y_t_df = pd.DataFrame(cur_test_y)
            neg_log_loss_results = multi_class_eval(
                marg_pred, y_t_df, neg_log_loss_with_labels, 'log_lik')

            neg_log_loss_results.to_csv(join(
                cur_save_dir, 'marginal_species_log_lik.csv'))

            pd.Series(neg_log_loss_results.mean()).to_csv(
                join(cur_save_dir, 'neg_log_loss_mean.csv'))

            fold_liks[i] = np.mean(log_liks)

            np.savez(join(cur_save_dir, 'cv_results'),
                     site_log_liks=log_liks,
                     cur_train_X=cur_X,
                     cur_train_y=cur_y,
                     cur_test_X=cur_test_x,
                     cur_test_y=cur_test_y,
                     train_ind=cur_train_ind,
                     test_ind=cur_test_ind)

        pd.Series({'mean_lik': np.mean(fold_liks)}).to_csv(
            join(save_dir, 'mean_lik.csv'))

        pd.Series(fold_liks, index=[
            f'fold_{i+1}' for i in range(n_folds)]).to_csv(
                join(save_dir, 'fold_liks.csv'))

        return np.mean(fold_liks), np.std(fold_liks) / np.sqrt(len(fold_liks))
