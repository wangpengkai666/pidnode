import torch
import torch.nn as nn
from torchdiffeq import odeint_adjoint as odeint
import numpy as np
from einops import rearrange, repeat
import time
import torch.optim as optim
import glob
import imageio
import numpy as np
import torch
from math import pi
from random import random
from torch.utils.data import Dataset, DataLoader
from torch.distributions import Normal
from torchvision import datasets, transforms
import argparse

import utils
import models


class DF(nn.Module):

    def __init__(self, in_channels, nhidden, out_channels=None, args=None):
        super(DF, self).__init__()
        self.args = args
        if self.args.model in (
        'node', 'anode', 'hbnode', 'ghbnode', 'node_ss', 'ghbnode_ss',
        'pidnode', 'gpidnode'):
            in_dim = in_channels
        if self.args.model == 'sonode':
            in_dim = 2 * in_channels
        self.activation = nn.ReLU(inplace=True)  # nn.LeakyReLU(0.3)
        self.fc1 = nn.Conv2d(in_dim + 1, nhidden, kernel_size=1, padding=0)
        self.fc2 = nn.Conv2d(nhidden + 1, nhidden, kernel_size=3, padding=1)
        self.fc3 = nn.Conv2d(nhidden + 1, in_channels, kernel_size=1, padding=0)

    def forward(self, t, x0):
        if self.args.model in ('hbnode', 'ghbnode', "pidnode", "gpidnode"):
            out = rearrange(x0, 'b 1 c x y -> b c x y')
        if self.args.model == 'anode':
            out = rearrange(x0, 'b d c x y -> b (d c) x y')
        if self.args.model in ('node', 'node_ss'):
            out = rearrange(x0, 'b d c x y -> b (d c) x y')
        if self.args.model == 'sonode':
            out = rearrange(x0, 'b d c x y -> b (d c) x y')
        t_img = torch.ones_like(out[:, :1, :, :]).to(device=self.args.gpu) * t
        out = torch.cat([out, t_img], dim=1)

        out = self.fc1(out)
        out = self.activation(out)
        out = torch.cat([out, t_img], dim=1)

        out = self.fc2(out)
        out = self.activation(out)
        out = torch.cat([out, t_img], dim=1)

        out = self.fc3(out)
        out = rearrange(out, 'b c x y -> b 1 c x y')
        if self.args.model == 'hbnode' or self.args.model == 'ghbnode' or self.args.model == 'ghbnode_ss':
            return out + self.args.xres * x0
        elif self.args.model in (
        'anode', 'sonode', 'node', 'node_ss', "pidnode", "gpidnode"):
            return out
        else:
            raise NotImplementedError


class NODEintegrate(nn.Module):

    def __init__(self, df=None, x0=None):
        """
        Create an OdeRnnBase model
            x' = df(x)
            x(t0) = x0
        :param df: a function that computes derivative. input & output shape [batch, channel, feature]
        :param x0: initial condition.
            - if x0 is set to be nn.parameter then it can be trained.
            - if x0 is set to be nn.Module then it can be computed through some network.
        """
        super().__init__()
        self.df = df
        self.x0 = x0

    def forward(self, initial_condition, evaluation_times, x0stats=None):
        """
        Evaluate odefunc at given evaluation time
        :param initial_condition: shape [batch, channel, feature]. Set to None while training.
        :param evaluation_times: time stamps where method evaluates, shape [time]
        :param x0stats: statistics to compute x0 when self.x0 is a nn.Module, shape required by self.x0
        :return: prediction by ode at evaluation_times, shape [time, batch, channel, feature]
        """
        if initial_condition is None:
            initial_condition = self.x0
        if x0stats is not None:
            initial_condition = self.x0(x0stats)
        out = odeint(self.df, initial_condition, evaluation_times, rtol=args.tol, atol=args.tol)
        return out

    @property
    def nfe(self):
        return self.df.nfe


class NODElayer(nn.Module):
    def __init__(self, df, args, evaluation_times=(0.0, 1.0), nesterov_algebraic=False, nesterov_factor=3, actv_k=None,
                 actv_output=None, method="dopri5", step_size=None):
        super(NODElayer, self).__init__()
        self.df = df
        self.evaluation_times = torch.as_tensor(evaluation_times)
        self.args = args
        self.nesterov_algebraic = nesterov_algebraic
        self.nesterov_factor = nesterov_factor
        self.actv_k = nn.Identity() if actv_k is None else actv_k
        self.actv_output = nn.Identity() if actv_output is None else actv_output
        self.method = method
        self.step_size = step_size

    def forward(self, x0):
        if self.method != "dopri5":
            out = odeint(self.df, x0, self.evaluation_times, rtol=self.args.tol, atol=self.args.tol, method=self.method,
                         options={"step_size": self.step_size})
        else:
            out = odeint(self.df, x0, self.evaluation_times, rtol=self.args.tol, atol=self.args.tol)
        if self.nesterov_algebraic:
            out = self.calc_algebraic_factor(out)
        return out[1]

    def to(self, device, *args, **kwargs):
        super().to(device, *args, **kwargs)
        self.evaluation_times.to(device)

    def calc_algebraic_factor(self, z):
        # split the input into the starting time step and the other time steps
        z_0 = z[:1]
        z_T = z[1:]
        # get the corresponding value of t for the other time steps
        if len(self.evaluation_times.shape) == 2:
            T = self.evaluation_times[:, 1:]
        else:
            T = self.evaluation_times[1:]
        if z.is_cuda and not T.is_cuda:
            # k = k.to(z.get_device())
            T = T.to(z.get_device())
        x, m = torch.split(z_T, 1, dim=2)
        # T^(-3/2) * e^(T/2)
        k = torch.pow(T, -self.nesterov_factor / 2) * torch.exp(T / 2)
        k = self.actv_k(k)
        # h(T) = [x(T) m(T)] * Transpose([T^(-3/2)*e^(T/2) I])
        h = self.actv_output(x * k)
        dh = self.actv_output(k * (m - (self.nesterov_factor / (2 * T * k) - 1 / 2 * 1 / k) * h))
        z_t = torch.cat((h, dh), dim=2)
        out = torch.cat((z_0, z_t), dim=0)
        return out


class NODE(nn.Module):
    def __init__(self, df=None, **kwargs):
        super(NODE, self).__init__()
        self.__dict__.update(kwargs)
        self.df = df
        self.nfe = 0

    def forward(self, t, x):
        self.nfe += 1
        return self.df(t, x)


class SONODE(NODE):
    def forward(self, t, x):
        """
        Compute [y y']' = [y' y''] = [y' df(t, y, y')]
        :param t: time, shape [1]
        :param x: [y y'], shape [batch, 2, vec]
        :return: [y y']', shape [batch, 2, vec]
        """
        self.nfe += 1
        v = x[:, 1:, :]
        out = self.df(t, x)
        return torch.cat((v, out), dim=1)


class HeavyBallNODE(NODE):
    def __init__(self, df, gamma=None, thetaact=None, gammaact='sigmoid', timescale=1):
        super().__init__(df)
        self.gamma = nn.Parameter(torch.Tensor([-3.0])) if gamma is None else gamma
        self.gammaact = nn.Sigmoid() if gammaact == 'sigmoid' else gammaact
        self.timescale = timescale
        self.thetaact = nn.Identity() if thetaact is None else thetaact

    def forward(self, t, x):
        """
        Compute [theta' m' v'] with heavy ball parametrization in
        $$ theta' = -m / sqrt(v + eps) $$
        $$ m' = h f'(theta) - rm $$
        $$ v' = p (f'(theta))^2 - qv $$
        https://www.jmlr.org/papers/volume21/18-808/18-808.pdf
        because v is constant, we change c -> 1/sqrt(v)
        c has to be positive
        :param t: time, shape [1]
        :param x: [theta m v], shape [batch, 3, dim]
        :return: [theta' m' v'], shape [batch, 3, dim]
        """
        self.nfe += 1
        theta, m = torch.split(x, 1, dim=1)
        dtheta = self.thetaact(-m)
        dm = self.df(t, theta) - self.timescale * torch.sigmoid(self.gamma) * m
        return torch.cat((dtheta, dm), dim=1)


class NesterovNODE(NODE):
    def __init__(self, df, thetaact=None, xi=None, nesterov_factor=3, actv_m=None, actv_df=None):
        super().__init__(df)
        self.sign = 1  # Sign of df
        self.thetaact = nn.Identity() if thetaact is None else thetaact  # Activation for dh, GNesterovNODE only
        self.xi = 0.0 if xi is None else xi  # residual term for General model
        self.actv_m = nn.Identity() if actv_m is None else actv_m  # Activation for dh, GNesterovNODE only
        self.actv_df = nn.Identity() if actv_df is None else actv_df  # Activation for df, GNesterovNODE only
        self.nesterov_factor = nesterov_factor
        # self.actv_dm = nn.Identity() if actv_dm is None else actv_dm # Activation for dh, GNesterovNODE only

    def forward(self, t, z):
        self.nfe += 1
        h, dh = torch.split(z, 1, dim=1)
        k_reciprocal = torch.pow(t, self.nesterov_factor / 2) * torch.exp(-t / 2)
        if z.is_cuda:
            k_reciprocal = k_reciprocal.to(z.get_device())
        m = (self.nesterov_factor / 2 * (1 / t) * k_reciprocal - 1 / 2 * k_reciprocal) * h \
            + k_reciprocal * dh
        x = h * k_reciprocal
        dx = self.thetaact(m)
        dm = self.actv_m(self.actv_df(self.df(t, h)) - m) - self.xi * h
        out = torch.cat((dx, dm), dim=1)
        return out


class HighNesterovNODE(NODE):
    """
    introduce the partial derivative of the gradient with respect to time as a operation to reduce oscillation.
    """

    def __init__(self, df, actv_h=None, corr=-100, corrf=True, gamma_guess=-3.0, gamma_act='sigmoid',
                 siga=0.2, use_h=False, full_details=False, nesterov_algebraic=True,
                 actv_m=None, actv_dm=None, actv_df=None, sign=1):
        super().__init__(df)
        self.gamma = nn.Parameter(torch.Tensor([gamma_guess]))
        self.gammaact = nn.Sigmoid() if gamma_act == 'sigmoid' else gamma_act
        self.corr = nn.Parameter(torch.Tensor([corr]))
        # 考虑是否需要学习
        self.siga = nn.Parameter(torch.Tensor([siga]))
        self.sp = nn.Softplus()
        self.sign = sign  # Sign of df
        self.actv_h = nn.Identity() if actv_h is None else actv_h  # Activation for dh, GNNODE only
        self.actv_m = nn.Identity() if actv_m is None else actv_m  # Activation for dh, GNNODE only
        self.actv_dm = nn.Identity() if actv_dm is None else actv_dm  # Activation for dh, GNNODE only
        self.actv_df = nn.Identity() if actv_df is None else actv_df  # Activation for df, GNNODE only
        self.use_h = use_h
        self.full_details = full_details
        self.nesterov_algebraic = nesterov_algebraic

    def forward(self, t, z):
        """
        Compute [x' m'] with diff-alg nesterov parametrization in
        $$ h' = -m + siga * df(t, h)$$
        $$ m' = -gamma * m + (1 - gamma * siga) * df(t, h) - xi * h $$
        :param t: time, shape [1]
        :param z: [h dh], shape [batch, 2, dim]
        :return: [x' m'], shape [batch, 2, dim]
        """
        self.nfe += 1
        h, m = torch.split(z, 1, dim=1)
        # dh = self.actv_h(m) + self.siga() * self.actv_df(self.df(t, h))

        dh = self.actv_h(m) + self.siga * self.actv_df(self.df(t, h))
        alfa = self.gammaact(self.gamma)
        dm = self.actv_dm(
            - alfa * m + (1 - alfa * self.siga) * self.actv_df(self.df(t, h)) - self.sp(self.corr) * h)
        # 合并全量的二阶变量参数
        out = torch.cat((dh, dm), dim=1)

        return out


class PIDNODE(NODE):
    def __init__(self, df, actv_h=None, gamma=-3.0, gammaact='sigmoid', corr=-100, corrf=True, sign=1,
                 kp=2, ki=1.5, kd=5, actv_m=None, actv_dm=None, actv_df=None, general_type=3):
        # the current optimal set of parameters is: kp=2 ki=1.5 kd=5
        super().__init__(df)
        # Momentum parameter gamma
        self.gamma = nn.Parameter(torch.Tensor([gamma]))
        self.gammaact = nn.Sigmoid() if gammaact == 'sigmoid' else gammaact
        self.kp = nn.Parameter(torch.Tensor([kp]))
        self.ki = nn.Parameter(torch.Tensor([ki]))
        self.kd = nn.Parameter(torch.Tensor([kd]))
        self.sp = nn.Softplus()
        self.sign = sign  # Sign of df
        self.actv_h = nn.Identity() if actv_h is None else actv_h  # Activation for dh, GHBNODE only
        self.actv_m = nn.Identity() if actv_m is None else actv_m  # Activation for dh, GNNODE only
        self.actv_dm = nn.Identity() if actv_dm is None else actv_dm  # Activation for dh, GNNODE only
        self.actv_df = nn.Identity() if actv_df is None else actv_df  # Activation for df, GNNODE only

        self.alpha_1 = nn.Parameter(torch.Tensor([-5.0]))
        self.alpha_2 = nn.Parameter(torch.Tensor([5.0]))
        self.epsilon = 1e-8
        self.act = nn.Softplus()

        # self.act = nn.ReLU()
        self.corr = nn.Parameter(torch.Tensor([corr]))
        # generalized type
        self.gt = general_type

    def forward(self, t, x):
        """
        Compute pid-hbnode
        $$ h' = -m $$
        $$ m' = -kp * h - (alfa + kd) * m - ki * v + f $$
        $$ v' = h $$
        based on paper https://www.jmlr.org/papers/volume21/18-808/18-808.pdf
        :param t: time, shape [1]
        :param x: [theta m], shape [batch, 2, dim]
        :return: [theta' m'], shape [batch, 2, dim]
        """

        self.nfe += 1

        # h, m, v = torch.split(x, 1, dim=1)
        h, m, v = torch.tensor_split(x, 3, dim=1)

        # import pdb; pdb.set_trace()
        # dh = self.actv_h(-m) / (torch.sqrt(torch.sigmoid(v))+ self.epsilon)
        # made a small lower limit guarantee to ensure that df does not undergo generalization, and performed certain initialization operations.
        dh = self.actv_h(m)
        if self.gt == 1:
            # type 1
            df = self.df(t, h)
            dm = self.actv_h(-self.kp * h - (self.gammaact(self.gamma) + self.kd) * m - self.ki * v) + df
            dv = h
        elif self.gt == 2:
            # type 2
            df = self.df(t, h)
            dm = -self.kp * h - (self.gammaact(self.gamma) + self.kd) * m - self.ki * v + df
            dv = self.actv_h(h)
        elif self.gt == 3:
            # type 3
            df = self.actv_df(self.df(t, h))
            dm = self.actv_h(-self.kp * h - (self.gammaact(self.gamma) + self.kd) * m - self.ki * v + df) - \
                 self.sp(self.corr) * h
            dv = self.actv_h(h)
        elif self.gt == 4:
            # type 4
            df = self.actv_df(self.df(t, h))
            dm = -self.kp * h - (self.gammaact(self.gamma) + self.kd) * m - self.ki * v + df
            dv = self.actv_h(h)
        elif self.gt == 5:
            # type 5
            df = self.actv_df(self.df(t, h))
            dm = -self.kp * h - (self.gammaact(self.gamma) + self.kd) * m - self.ki * v + df
            dv = h
        else:
            raise NotImplementedError

        # no general type
        # dh = m
        # df = self.df(t, h)
        # dm = -self.kp*h-(self.gammaact(self.gamma())+self.kd)*m\
        #      -self.ki*v+df
        # dv = h
        # dm = dm + self.sp(self.corr()) * h
        # dv = dv + self.sp(self.corr2()) * h

        out = torch.cat((dh, dm, dv), dim=1)

        return out


class initial_velocity(nn.Module):

    def __init__(self, in_channels, out_channels, nhidden):
        super(initial_velocity, self).__init__()
        assert (3 * out_channels >= in_channels)
        self.actv = nn.LeakyReLU(0.3)
        self.fc1 = nn.Conv2d(in_channels, nhidden, kernel_size=1, padding=0)
        self.fc2 = nn.Conv2d(nhidden, nhidden, kernel_size=3, padding=1)
        self.fc3 = nn.Conv2d(nhidden, 2 * out_channels - in_channels, kernel_size=1, padding=0)
        self.out_channels = out_channels
        self.in_channels = in_channels

    def forward(self, x0):
        x0 = x0.float()
        out = self.fc1(x0)
        out = self.actv(out)
        out = self.fc2(out)
        out = self.actv(out)
        out = self.fc3(out)
        out = torch.cat([x0, out], dim=1)
        out = rearrange(out, 'b (d c) ... -> b d c ...', d=2)
        return out


class anode_initial_velocity(nn.Module):

    def __init__(self, in_channels, aug, args):
        super(anode_initial_velocity, self).__init__()
        self.args = args
        self.aug = aug
        self.in_channels = in_channels

    def forward(self, x0):
        x0 = rearrange(x0.float(), 'b c x y -> b 1 c x y')
        outshape = list(x0.shape)
        outshape[2] = self.aug
        out = torch.zeros(outshape).to(self.args.gpu)
        out[:, :, :3] += x0
        return out

class pidnode_initial_velocity(nn.Module):

    def __init__(self, in_channels, out_channels, nhid):
        super(pidnode_initial_velocity, self).__init__()
        assert (3 * out_channels >= in_channels)
        self.actv = nn.LeakyReLU(0.3)
        self.fc1 = nn.Conv2d(in_channels, nhid, kernel_size=1, padding=0)
        self.fc2 = nn.Conv2d(nhid, nhid, kernel_size=3, padding=1)
        self.fc3 = nn.Conv2d(nhid, 3 * out_channels - in_channels, kernel_size=1, padding=0)
        self.out_channels = out_channels
        self.in_channels = in_channels

    def forward(self, x0):
        x0 = x0.float()
        out = self.fc1(x0)
        out = self.actv(out)
        out = self.fc2(out)
        out = self.actv(out)
        out = self.fc3(out)
        # import pdb; pdb.set_trace()
        out = torch.cat([x0, out], dim=1)
        out = rearrange(out, 'b (d c) ... -> b d c ...', d=3)
        # import pdb; pdb.set_trace()
        return out

class predictionlayer(nn.Module):
    def __init__(self, in_channels):
        super(predictionlayer, self).__init__()
        self.dense = nn.Linear(in_channels * 32 * 32, 10)
        # self.dropout = nn.Dropout(0.1)

    def forward(self, x):
        x = rearrange(x[:, 0], 'b c x y -> b (c x y)')
        # x = self.dropout(x)
        x = self.dense(x)
        return x
