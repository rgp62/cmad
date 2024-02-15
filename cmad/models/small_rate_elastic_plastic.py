import numpy as np
import jax.numpy as jnp

from jax import grad

from functools import partial

from cmad.models.deformation_types import DefType, def_type_ndims
from cmad.models.elastic_stress import (isotropic_linear_elastic_stress,
                                        two_mu_scale_factor)
from cmad.models.effective_stress import effective_stress_fun
from cmad.models.hardening import combined_hardening_fun, get_hardening_funs
from cmad.models.kinematics import gather_F
from cmad.models.model import Model
from cmad.parameters.parameters import Parameters
from cmad.models.paths import cond_residual
from cmad.models.var_types import (
    VarType,
    get_num_eqs,
    get_scalar,
    get_sym_tensor_from_vector,
    get_vector_from_sym_tensor,
    get_tensor_from_vector)


def compute_delta_strain(xi, xi_prev, params, u, u_prev, def_type):

    local_var_idx = 2
    F = gather_F(xi, u, def_type, local_var_idx)
    F_prev = gather_F(xi_prev, u_prev, def_type, local_var_idx)

    I = jnp.eye(3)
    grad_u = F - I
    grad_u_prev = F_prev - I

    epsilon = 0.5 * (grad_u + grad_u.T)
    epsilon_prev = 0.5 * (grad_u_prev + grad_u_prev.T)

    return epsilon - epsilon_prev


def compute_yield_fun_and_normal(xi, params, def_type,
                                 effective_stress, hardening):

    ndims = def_type_ndims(def_type)

    plastic_params = params["plastic"]
    Y = plastic_params["flow stress"]["initial yield"]["Y"]
    hardening_params = plastic_params["flow stress"]["hardening"]

    cauchy = get_sym_tensor_from_vector(xi[0], 3)
    phi = effective_stress(cauchy, plastic_params)

    alpha = get_scalar(xi[1])
    sigma_flow = Y + hardening(alpha, hardening_params)

    yield_fun = (phi - sigma_flow) / two_mu_scale_factor(params)
    yield_normal = grad(effective_stress)(cauchy, plastic_params)

    return yield_fun, yield_normal


class SmallRateElasticPlastic(Model):
    """
    Small strain rate form elastic-plastic model:
    Elastic: Modular linear elasticity
    Plastic: Modular effective stress and hardening
    """

    def __init__(self, parameters: Parameters,
                 def_type=DefType.FULL_3D,
                 elastic_stress_fun=isotropic_linear_elastic_stress,
                 hardening_funs: dict = get_hardening_funs(),
                 yield_tol=1e-14):

        self._def_type = def_type
        ndims = def_type_ndims(def_type)
        self._ndims = ndims

        if def_type == DefType.FULL_3D:
            num_residuals = 2

        elif def_type == DefType.PLANE_STRESS \
                or def_type == DefType.UNIAXIAL_STRESS:
            num_residuals = 3

        else:
            raise NotImplementedError

        self._init_residuals(num_residuals)

        # cauchy stress tensor
        self.resid_names[0] = "cauchy"
        self._var_types[0] = VarType.SYM_TENSOR
        self._num_eqs[0] = get_num_eqs(VarType.SYM_TENSOR, 3)
        init_vec_cauchy = np.zeros(self._num_eqs[0])

        # isotropic hardening variable
        self.resid_names[1] = "alpha"
        self._var_types[1] = VarType.SCALAR
        self._num_eqs[1] = get_num_eqs(VarType.SCALAR, 3)
        init_alpha = np.zeros(self._num_eqs[1])

        self._init_xi = [init_vec_cauchy, init_alpha]

        if def_type == DefType.PLANE_STRESS:
            # out of plane stretch
            self.resid_names[2] = "out of plane stretch"
            self._var_types[2] = VarType.SCALAR
            self._num_eqs[2] = get_num_eqs(VarType.SCALAR, ndims)
            init_oop_stretch = np.ones(self._num_eqs[2])

            self._init_xi += [init_oop_stretch]

        # may want to allow for some idx ([0, 1 ,2]) to be the uniaxial
        # stress idx later
        elif def_type == DefType.UNIAXIAL_STRESS:
            # off-axis stretches
            self.resid_names[2] = "off-axis stretches"
            self._var_types[2] = VarType.VECTOR
            self._num_eqs[2] = get_num_eqs(VarType.VECTOR, 2)
            init_off_axis_stretches = np.ones(self._num_eqs[2])

            self._init_xi += [init_off_axis_stretches]

        # set the initial values for xi and xi_prev
        self._init_state_variables()
        self.set_xi_to_init_vals()

        # TODO: check that the parameters make sense for this model
        # self._check_params(parameters)
        self.parameters = parameters

        effective_stress_type = \
            list(parameters.values["plastic"]["effective stress"])[0]

        residual = partial(self._residual, def_type=def_type,
                           elastic_stress=elastic_stress_fun,
                           effective_stress=effective_stress_fun(
                               effective_stress_type),
                           hardening=partial(combined_hardening_fun,
                                             hardening_funs=hardening_funs),
                           yield_tol=yield_tol)

        cauchy = partial(self.cauchy, def_type=def_type)

        super().__init__(residual, cauchy)

    @staticmethod
    def _residual(xi, xi_prev, params, u, u_prev,
                  def_type, elastic_stress, effective_stress, hardening,
                  yield_tol) -> jnp.array:

        # state variables for the model
        cauchy = get_sym_tensor_from_vector(xi[0], 3)
        cauchy_prev = get_sym_tensor_from_vector(xi_prev[0], 3)
        alpha = get_scalar(xi[1])
        alpha_prev = get_scalar(xi_prev[1])

        trial_delta_strain \
            = compute_delta_strain(xi, xi_prev, params, u, u_prev, def_type)
        trial_delta_cauchy \
            = elastic_stress(trial_delta_strain, params)
        delta_gamma = alpha - alpha_prev
        scale_factor = two_mu_scale_factor(params)

        # elastic residual
        C_elastic_cauchy_tensor = cauchy - cauchy_prev \
            - trial_delta_cauchy
        C_elastic_cauchy = \
            get_vector_from_sym_tensor(C_elastic_cauchy_tensor, 3) \
            / scale_factor
        C_elastic_alpha = delta_gamma

        # plastic residual
        yield_fun, yield_normal = \
            compute_yield_fun_and_normal(xi, params, def_type,
                                         effective_stress, hardening)
        delta_plastic_strain = delta_gamma * yield_normal
        delta_cauchy = trial_delta_cauchy \
            - elastic_stress(delta_plastic_strain, params)
        C_plastic_cauchy_tensor = cauchy - cauchy_prev \
            - delta_cauchy
        C_plastic_cauchy = \
            get_vector_from_sym_tensor(C_plastic_cauchy_tensor, 3) \
            / scale_factor
        C_plastic_alpha = yield_fun

        if def_type == def_type.FULL_3D:
            C_elastic = jnp.r_[C_elastic_cauchy, C_elastic_alpha]
            C_plastic = jnp.r_[C_plastic_cauchy, C_plastic_alpha]

        elif def_type == def_type.PLANE_STRESS or \
                def_type == def_type.UNIAXIAL_STRESS:

            if def_type == def_type.PLANE_STRESS:
                C_elastic_stretch = trial_delta_cauchy[2, 2] / scale_factor
                C_plastic_stretch = delta_cauchy[2, 2] / scale_factor

            elif def_type == def_type.UNIAXIAL_STRESS:
                C_elastic_stretch = jnp.r_[trial_delta_cauchy[1, 1],
                                           trial_delta_cauchy[2, 2]] / scale_factor
                C_plastic_stretch = jnp.r_[delta_cauchy[1, 1],
                                           delta_cauchy[2, 2]] / scale_factor

            C_elastic = jnp.r_[C_elastic_cauchy, C_elastic_alpha,
                               C_elastic_stretch]
            C_plastic = jnp.r_[C_plastic_cauchy, C_plastic_alpha,
                               C_plastic_stretch]

        return cond_residual(yield_fun, C_elastic, C_plastic, yield_tol)

    def _check_params(self, parameters):
        raise NotImplementedError

    @staticmethod
    def cauchy(xi, xi_prev, params, u, u_prev, def_type) -> jnp.array:

        return get_sym_tensor_from_vector(xi[0], 3)
