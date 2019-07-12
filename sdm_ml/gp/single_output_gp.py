import os
import gpflow
import numpy as np
from tqdm import tqdm
from os.path import join

from sklearn.preprocessing import StandardScaler
from sdm_ml.presence_absence_model import PresenceAbsenceModel
from .utils import (find_starting_z, calculate_log_joint_bernoulli_likelihood,
                    save_gpflow_model, log_probability_via_sampling)


class SingleOutputGP(PresenceAbsenceModel):

    def __init__(self, n_inducing, kernel_function, maxiter=int(1E6),
                 verbose_fit=True, n_draws_predict=int(1E4)):

        self.models = None
        self.is_fit = False
        self.kernel_function = kernel_function
        self.n_inducing = n_inducing
        self.maxiter = maxiter
        self.verbose_fit = verbose_fit
        self.n_draws_predict = n_draws_predict
        self.scaler = None

    @staticmethod
    def build_default_kernel(n_dims, add_bias=True):
        # This can be curried to produce the kernel function required.
        kernel = gpflow.kernels.RBF(input_dim=n_dims, ARD=True)
        kernel += gpflow.kernels.Bias(1)

        return kernel

    def fit(self, X, y):

        self.scaler = StandardScaler()
        X = self.scaler.fit_transform(X)

        Z = find_starting_z(X, num_inducing=self.n_inducing,
                            use_minibatching=False)

        self.models = list()

        # We need to fit each species separately
        for cur_output in tqdm(range(y.shape[1])):

            cur_kernel = self.kernel_function()
            cur_likelihood = gpflow.likelihoods.Bernoulli()

            cur_y = y[:, [cur_output]]

            cur_m = gpflow.models.SVGP(X, cur_y, kern=cur_kernel,
                                       likelihood=cur_likelihood, Z=Z)

            opt = gpflow.train.ScipyOptimizer(
                options={'maxfun': self.maxiter})

            opt.minimize(cur_m, maxiter=self.maxiter, disp=self.verbose_fit)

            self.models.append(cur_m)

        self.is_fit = True

    def predict_log_marginal_probabilities(self, X: np.ndarray) -> np.ndarray:
        # TODO: Check against GPFlow.
        # TODO: Is this really worth it? Could just use predict_y.

        assert self.is_fit

        X = self.scaler.transform(X)

        # Run the prediction for each model
        results = list()

        for cur_model in self.models:

            # Predict f, the latent probability on the probit scale
            f_mean, f_var = cur_model.predict_f(X)
            f_std = np.sqrt(f_var)

            result = log_probability_via_sampling(
                np.squeeze(f_mean), np.squeeze(f_std), self.n_draws_predict)

            results.append(result)

        results = np.stack(results, axis=1)

        return results

    def calculate_log_likelihood(self, X, y):

        assert self.is_fit

        assert y.shape[1] == len(self.models)

        X = self.scaler.transform(X)

        means, sds = list(), list()

        for cur_model in self.models:

            cur_mean, cur_vars = cur_model.predict_f(X)
            cur_sds = np.sqrt(cur_vars)

            means.append(cur_mean)
            sds.append(cur_sds)

        means = np.stack(means, axis=1)
        sds = np.stack(sds, axis=1)

        site_log_liks = np.zeros(means.shape[0])

        # Estimate site by site
        for i, (cur_y, cur_mean, cur_sd) in enumerate(zip(y, means, sds)):

            draws = np.random.normal(
                cur_mean, cur_sd, size=(self.n_draws_predict, means.shape[1]))

            log_lik = calculate_log_joint_bernoulli_likelihood(draws, cur_y)

            site_log_liks[i] = log_lik

        return site_log_liks

    def save_model(self, target_folder):

        os.makedirs(target_folder, exist_ok=True)

        for i, cur_model in enumerate(self.models):

            target_file = join(target_folder, f'model_species_{i}')

            save_gpflow_model(cur_model, target_file)
