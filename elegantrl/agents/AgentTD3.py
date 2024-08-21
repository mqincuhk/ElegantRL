import numpy as np
import torch as th
from typing import Tuple, List
from copy import deepcopy

from elegantrl.train.config import Config
from elegantrl.train.replay_buffer import ReplayBuffer
from elegantrl.agents.AgentBase import AgentBase, ActorBase, CriticBase
from elegantrl.agents.AgentBase import build_mlp, layer_init_with_orthogonal

TEN = th.Tensor


class AgentTD3(AgentBase):
    """Twin Delayed DDPG algorithm.
    Addressing Function Approximation Error in Actor-Critic Methods. 2018.
    """

    def __init__(self, net_dims: [int], state_dim: int, action_dim: int, gpu_id: int = 0, args: Config = Config()):
        super().__init__(net_dims, state_dim, action_dim, gpu_id, args)
        self.update_freq = getattr(args, 'update_freq', 2)  # standard deviation of exploration noise
        self.num_ensembles = getattr(args, 'num_ensembles', 8)  # the number of critic networks
        self.policy_noise_std = getattr(args, 'policy_noise_std', 0.10)  # standard deviation of exploration noise
        self.explore_noise_std = getattr(args, 'explore_noise_std', 0.05)  # standard deviation of exploration noise

        self.act = Actor(net_dims, state_dim, action_dim).to(self.device)
        self.cri = CriticTwin(net_dims, state_dim, action_dim, num_ensembles=self.num_ensembles).to(self.device)
        self.act_target = deepcopy(self.act)
        self.cri_target = deepcopy(self.cri)
        self.act_optimizer = th.optim.Adam(self.act.parameters(), self.learning_rate)
        self.cri_optimizer = th.optim.Adam(self.cri.parameters(), self.learning_rate)

    def _update_objectives_raw(self, buffer: ReplayBuffer, batch_size: int, update_t: int) -> Tuple[float, float]:
        assert isinstance(update_t, int)
        with th.no_grad():
            state, action, reward, undone, unmask, next_state = buffer.sample(batch_size)

            next_action = self.act.get_action(next_state, action_std=self.policy_noise_std)  # deterministic policy
            next_q = self.cri_target.get_q_values(next_state, next_action).min(dim=1, keepdim=True)[0]

            q_label = reward + undone * self.gamma * next_q

        q_values = self.cri.get_q_values(state, action)
        q_labels = q_label.repeat(1, q_values.shape[1])
        obj_critic = (self.criterion(q_values, q_labels) * unmask).mean()
        self.optimizer_backward(self.cri_optimizer, obj_critic)
        self.soft_update(self.cri_target, self.cri, self.soft_update_tau)

        if update_t % self.update_freq == 0:  # delay update
            action_pg = self.act(state)  # action to policy gradient
            obj_actor = self.cri(state, action_pg).mean()
            self.optimizer_backward(self.act_optimizer, -obj_actor)
            self.soft_update(self.act_target, self.act, self.soft_update_tau)
        else:
            obj_actor = th.tensor(th.nan)
        return obj_critic.item(), obj_actor.item()

    def _update_objectives_per(self, buffer: ReplayBuffer, batch_size: int, update_t: int) -> Tuple[float, float]:
        assert isinstance(update_t, int)
        with th.no_grad():
            state, action, reward, undone, unmask, next_state, is_weight, is_index = buffer.sample_for_per(batch_size)

            next_action = self.act.get_action(next_state, action_std=self.policy_noise_std)  # deterministic policy
            next_q = self.cri_target.get_q_values(next_state, next_action).min(dim=1, keepdim=True)[0]

            q_label = reward + undone * self.gamma * next_q

        q_values = self.cri.get_q_values(state, action)
        q_labels = q_label.repeat(1, q_values.shape[1])
        td_error = self.criterion(q_values, q_labels).mean(dim=-1, keepdim=True) * unmask
        obj_critic = (td_error * is_weight).mean()
        self.optimizer_backward(self.cri_optimizer, obj_critic)
        self.soft_update(self.cri_target, self.cri, self.soft_update_tau)
        buffer.td_error_update_for_per(is_index.detach(), td_error.detach())

        if update_t % self.update_freq == 0:  # delay update
            action_pg = self.act(state)  # action to policy gradient
            obj_actor = self.cri(state, action_pg).mean()
            self.optimizer_backward(self.act_optimizer, -obj_actor)
            self.soft_update(self.act_target, self.act, self.soft_update_tau)
        else:
            obj_actor = th.tensor(th.nan)
        return obj_critic.item(), obj_actor.item()


'''network'''


class Actor(ActorBase):
    def __init__(self, net_dims: List[int], state_dim: int, action_dim: int):
        super().__init__(state_dim=state_dim, action_dim=action_dim)
        self.net = build_mlp(dims=[state_dim, *net_dims, action_dim])
        layer_init_with_orthogonal(self.net[-1], std=0.1)

        self.explore_noise_std = 0.01  # standard deviation of exploration action noise
        self.ActionDist = th.distributions.normal.Normal

    def forward(self, state: TEN) -> TEN:
        state = self.state_norm(state)
        action = self.net(state)
        return action.tanh()

    def get_action(self, state: TEN, action_std: float) -> TEN:  # for exploration
        state = self.state_norm(state)
        action_avg = self.net(state).tanh()
        dist = self.ActionDist(action_avg, action_std)
        action = dist.sample()
        return action.clip(-1.0, 1.0)


class CriticTwin(CriticBase):  # shared parameter
    def __init__(self, net_dims: List[int], state_dim: int, action_dim: int, num_ensembles: int = 2):
        super().__init__(state_dim=state_dim, action_dim=action_dim)
        self.net = build_mlp(dims=[state_dim + action_dim, *net_dims, num_ensembles])
        layer_init_with_orthogonal(self.net[-1], std=0.5)

    def forward(self, state: TEN, action: TEN) -> TEN:
        state = self.state_norm(state)
        values = self.get_q_values(state=state, action=action)
        value = values.mean(dim=-1, keepdim=True)
        return value  # Q value

    def get_q_values(self, state: TEN, action: TEN) -> TEN:
        state = self.state_norm(state)
        values = self.net(th.cat((state, action), dim=1))
        return values  # Q values
