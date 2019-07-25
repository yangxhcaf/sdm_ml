import os
import gpflow
import numpy as np
from os.path import join
from functools import partial

from sdm_ml.dataset import BBSDataset, SpeciesData
from sdm_ml.norberg_dataset import NorbergDataset
from sdm_ml.scikit_model import ScikitModel
from sdm_ml.evaluation import compute_and_save_results_for_evaluation
from sdm_ml.gp.single_output_gp import SingleOutputGP
from sdm_ml.gp.multi_output_gp import MultiOutputGP
from ml_tools.utils import create_path_with_variables


def evaluate_model(training_set, test_set, model, output_dir):

    np.save(join(output_dir, 'names'), test_set.outcomes.columns.values)

    model.fit(training_set.covariates.values,
              training_set.outcomes.values.astype(int))

    compute_and_save_results_for_evaluation(test_set, model, output_dir)


def get_single_output_gp(n_dims, n_outcomes, test_run, add_bias, add_priors,
                         n_inducing):

    # Fetch single output GP
    default_kernel_fun = partial(
        SingleOutputGP.build_default_kernel, n_dims=n_dims, add_bias=add_bias,
        add_priors=add_priors)

    so_gp = SingleOutputGP(
        n_inducing=n_inducing, kernel_function=default_kernel_fun,
        verbose_fit=False, maxiter=10 if test_run else int(1E6))

    return so_gp


def get_multi_output_gp(n_dims, n_outcomes, n_kernels, n_inducing, add_bias,
                        use_priors, test_run, use_mean_function):

    if use_mean_function:
        mean_fun = partial(MultiOutputGP.build_default_mean_function,
                           n_outputs=n_outcomes)
    else:
        mean_fun = lambda: None # NOQA

    # Fetch multi output GP
    mogp_kernel = MultiOutputGP.build_default_kernel(
        n_dims=n_dims, n_kernels=n_kernels, n_outputs=n_outcomes,
        add_bias=add_bias, priors=use_priors)

    mogp = MultiOutputGP(n_inducing=n_inducing, n_latent=n_kernels,
                         kernel=mogp_kernel, maxiter=10 if test_run else
                         int(1E6), mean_function=mean_fun)

    return mogp


def get_log_reg(n_dims, n_outcomes):

    model = ScikitModel()

    return model


def get_random_forest_cv(n_dims, n_outcomes):

    model = ScikitModel(ScikitModel.create_cross_validated_forest)

    return model


def pick_random_species_and_sites(n_sites_to_pick, n_species_to_pick,
                                  n_sites, n_species):

    assert n_sites_to_pick < n_sites and n_species_to_pick < n_species

    picked_sites = np.random.choice(n_sites, size=n_sites_to_pick,
                                    replace=False)
    picked_species = np.random.choice(n_species, size=n_species_to_pick,
                                      replace=False)

    return picked_sites, picked_species


def reduce_sites(species_data, picked_sites):

    new_lat_lon = (species_data.lat_lon if species_data.lat_lon is None
                   else species_data.lat_lon.iloc[picked_sites])

    return SpeciesData(
        covariates=species_data.covariates.iloc[picked_sites],
        outcomes=species_data.outcomes.iloc[picked_sites],
        lat_lon=new_lat_lon)


def reduce_species(species_data, picked_species):

    return SpeciesData(
        covariates=species_data.covariates,
        outcomes=species_data.outcomes.iloc[:, picked_species],
        lat_lon=species_data.lat_lon)


if __name__ == '__main__':

    test_run = True
    output_base_dir = './experiments/evaluations/'

    datasets = NorbergDataset.fetch_all_norberg_sets()
    datasets['bbs'] = BBSDataset.init_using_env_variable()

    models = {
        'rf_cv': get_random_forest_cv,
        'mogp': partial(get_multi_output_gp, n_inducing=100,
                        n_kernels=10, add_bias=True, use_priors=True,
                        test_run=test_run, use_mean_function=False),
        'log_reg_cv': get_log_reg,
        'sogp': partial(get_single_output_gp, test_run=test_run,
                        add_bias=True, add_priors=True,
                        n_inducing=100),
    }

    target_dir = join(output_base_dir,
                      create_path_with_variables(test_run=test_run))

    for cur_dataset_name, cur_dataset in datasets.items():

        training_set = cur_dataset.training_set
        test_set = cur_dataset.test_set

        n_dims = training_set.covariates.shape[1]
        n_outcomes = training_set.outcomes.shape[1]

        if test_run:

            n_sites = training_set.covariates.shape[0]

            sites_to_pick = 100
            species_to_pick = 10

            picked_sites, picked_species = pick_random_species_and_sites(
                sites_to_pick, species_to_pick, n_sites, n_outcomes)

            training_set = reduce_sites(training_set, picked_sites)
            training_set = reduce_species(training_set, picked_species)
            test_set = reduce_species(test_set, picked_species)

            n_outcomes = species_to_pick

        subdir = join(target_dir, cur_dataset_name)

        for cur_model_name, cur_model_fn in models.items():

            # Make sure tf graph is clear
            gpflow.reset_default_graph_and_session()

            cur_subdir = join(subdir, cur_model_name)
            os.makedirs(cur_subdir, exist_ok=True)

            cur_model = cur_model_fn(n_dims, n_outcomes)

            try:
                evaluate_model(training_set, test_set, cur_model, cur_subdir)
            except ValueError as e:
                print(f'Failed to fit {cur_model_name}. Error was: {e}')
                target_file = join(cur_subdir, 'error.txt')
                with open(target_file, 'w') as f:
                    f.write(f'Failed to fit model. Error was: {e}')
                continue
