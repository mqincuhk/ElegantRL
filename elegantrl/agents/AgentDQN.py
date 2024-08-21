import torch as th

from typing import Tuple
from copy import deepcopy
from torch import nn

from elegantrl.agents.AgentBase import AgentBase, layer_init_with_orthogonal
from elegantrl.agents.AgentBase import build_mlp
from elegantrl.train.config import Config
from elegantrl.train.replay_buffer import ReplayBuffer

TEN = th.Tensor


class AgentDQN(AgentBase):
    """Deep Q-Network algorithm.
    “Human-Level Control Through Deep Reinforcement Learning”. Mnih V. et al.. 2015.
    """

    def __init__(self, net_dims: [int], state_dim: int, action_dim: int, gpu_id: int = 0, args: Config = Config()):
        super().__init__(net_dims=net_dims, state_dim=state_dim, action_dim=action_dim, gpu_id=gpu_id, args=args)

        self.act = self.cri = QNetwork(net_dims=net_dims, state_dim=state_dim, action_dim=action_dim).to(self.device)
        self.act_target = self.cri_target = deepcopy(self.act)
        self.act_optimizer = self.cri_optimizer = th.optim.Adam(self.act.parameters(), self.learning_rate)

        self.explore_rate = getattr(args, "explore_rate", 0.25)  # set for `self.act.get_action()`
        # the probability of choosing action randomly in epsilon-greedy

    def _explore_one_action(self, state: TEN) -> TEN:
        return self.act.get_action(state.unsqueeze(0), explore_rate=self.explore_rate)[0, 0]

    def _explore_vec_action(self, state: TEN) -> TEN:
        return self.act.get_action(state, explore_rate=self.explore_rate)[:, 0]

    def _update_objectives_raw(self, buffer: ReplayBuffer, batch_size: int, update_t: int) -> Tuple[float, float]:
        assert isinstance(update_t, int)
        with th.no_grad():
            state, action, reward, undone, unmask, next_state = buffer.sample(batch_size)

            next_q = self.cri_target(next_state).max(dim=1, keepdim=True)[0].squeeze(1)  # next q_values
            q_label = reward + undone * self.gamma * next_q

        q_value = self.cri(state).gather(1, action.long()).squeeze(1)
        obj_critic = (self.criterion(q_value, q_label) * unmask).mean()
        self.optimizer_backward(self.cri_optimizer, obj_critic)
        self.soft_update(self.cri_target, self.cri, self.soft_update_tau)

        obj_actor = q_value.detach().mean()
        return obj_critic.item(), obj_actor.item()

    def _update_objectives_per(self, buffer: ReplayBuffer, batch_size: int, update_t: int) -> Tuple[float, float]:
        assert isinstance(update_t, int)
        with th.no_grad():
            state, action, reward, undone, unmask, next_state, is_weight, is_index = buffer.sample_for_per(batch_size)

            next_q = self.cri_target(next_state).max(dim=1, keepdim=True)[0].squeeze(1)  # next q_values
            q_label = reward + undone * self.gamma * next_q

        q_value = self.cri(state).gather(1, action.long()).squeeze(1)
        td_error = self.criterion(q_value, q_label) * unmask
        obj_critic = (td_error * is_weight).mean()
        self.optimizer_backward(self.cri_optimizer, obj_critic)
        self.soft_update(self.cri_target, self.cri, self.soft_update_tau)
        buffer.td_error_update_for_per(is_index.detach(), td_error.detach())

        obj_actor = q_value.detach().mean()
        return obj_critic.item(), obj_actor.item()

    def get_cumulative_rewards(self, rewards: TEN, undones: TEN) -> TEN:
        returns = th.empty_like(rewards)

        masks = undones * self.gamma
        horizon_len = rewards.shape[0]

        last_state = self.last_state
        next_value = self.act_target(last_state).argmax(dim=1).detach()  # actor is Q Network in DQN style
        for t in range(horizon_len - 1, -1, -1):
            returns[t] = next_value = rewards[t] + masks[t] * next_value
        return returns


class AgentDoubleDQN(AgentDQN):
    """
    Double Deep Q-Network algorithm. “Deep Reinforcement Learning with Double Q-learning”. H. V. Hasselt et al.. 2015.
    """

    def __init__(self, net_dims: [int], state_dim: int, action_dim: int, gpu_id: int = 0, args: Config = Config()):
        self.act_class = getattr(self, "act_class", QNetTwin)
        self.cri_class = getattr(self, "cri_class", None)  # means `self.cri = self.act`
        super().__init__(net_dims=net_dims, state_dim=state_dim, action_dim=action_dim, gpu_id=gpu_id, args=args)

    def _update_objectives_raw(self, buffer: ReplayBuffer, batch_size: int, update_t: int) -> Tuple[float, float]:
        assert isinstance(update_t, int)
        with th.no_grad():
            state, action, reward, undone, unmask, next_state = buffer.sample(batch_size)

            next_q = th.min(*self.cri_target.get_q1_q2(next_state)).max(dim=1, keepdim=True)[0].squeeze(1)
            q_label = reward + undone * self.gamma * next_q

        q_value1, q_value2 = [qs.gather(1, action.long()).squeeze(1) for qs in self.act.get_q1_q2(state)]
        obj_critic = ((self.criterion(q_value1, q_label) + self.criterion(q_value2, q_label)) * unmask).mean()
        self.optimizer_backward(self.cri_optimizer, obj_critic)
        self.soft_update(self.cri_target, self.cri, self.soft_update_tau)

        obj_actor = q_value1.detach().mean()
        return obj_critic.item(), obj_actor.item()

    def _update_objectives_per(self, buffer: ReplayBuffer, batch_size: int, update_t: int) -> Tuple[float, float]:
        assert isinstance(update_t, int)
        with th.no_grad():
            state, action, reward, undone, unmask, next_state, is_weight, is_index = buffer.sample_for_per(batch_size)

            next_q = th.min(*self.cri_target.get_q1_q2(next_state)).max(dim=1, keepdim=True)[0].squeeze(1)
            q_label = reward + undone * self.gamma * next_q

        q_value1, q_value2 = [qs.gather(1, action.long()).squeeze(1) for qs in self.act.get_q1_q2(state)]
        td_error = (self.criterion(q_value1, q_label) + self.criterion(q_value2, q_label)) * unmask
        obj_critic = (td_error * is_weight).mean()
        self.optimizer_backward(self.cri_optimizer, obj_critic)
        self.soft_update(self.cri_target, self.cri, self.soft_update_tau)
        buffer.td_error_update_for_per(is_index.detach(), td_error.detach())

        obj_actor = q_value1.detach().mean()
        return obj_critic.item(), obj_actor.item()


'''add dueling q network'''


class AgentDuelingDQN(AgentDQN):
    def __init__(self, net_dims: [int], state_dim: int, action_dim: int, gpu_id: int = 0, args: Config = Config()):
        self.act_class = getattr(self, "act_class", QNetDuel)
        self.cri_class = getattr(self, "cri_class", None)  # means `self.cri = self.act`
        super().__init__(net_dims=net_dims, state_dim=state_dim, action_dim=action_dim, gpu_id=gpu_id, args=args)


class AgentD3QN(AgentDoubleDQN):  # Dueling Double Deep Q Network. (D3QN)
    def __init__(self, net_dims: [int], state_dim: int, action_dim: int, gpu_id: int = 0, args: Config = Config()):
        self.act_class = getattr(self, "act_class", QNetTwinDuel)
        self.cri_class = getattr(self, "cri_class", None)  # means `self.cri = self.act`
        super().__init__(net_dims=net_dims, state_dim=state_dim, action_dim=action_dim, gpu_id=gpu_id, args=args)


'''network'''


class QNetBase(nn.Module):  # nn.Module is a standard PyTorch Network
    def __init__(self, state_dim: int, action_dim: int):
        super().__init__()
        self.net = None  # build_mlp(net_dims=[state_dim + action_dim, *net_dims, 1])
        self.state_dim = state_dim
        self.action_dim = action_dim

        self.state_avg = nn.Parameter(th.zeros((state_dim,)), requires_grad=False)
        self.state_std = nn.Parameter(th.ones((state_dim,)), requires_grad=False)

    def state_norm(self, state: TEN) -> TEN:
        return (state - self.state_avg) / self.state_std


class QNetwork(QNetBase):
    def __init__(self, net_dims: [int], state_dim: int, action_dim: int):
        super().__init__(state_dim=state_dim, action_dim=action_dim)
        self.net = build_mlp(dims=[state_dim, *net_dims, action_dim])
        layer_init_with_orthogonal(self.net[-1], std=0.1)

    def forward(self, state):
        state = self.state_norm(state)
        value = self.net(state)
        return value  # Q values for multiple actions

    def get_action(self, state: TEN, explore_rate: float):  # return the index List[int] of discrete action
        state = self.state_norm(state)
        if explore_rate < th.rand(1):
            action = self.net(state).argmax(dim=1, keepdim=True)
        else:
            action = th.randint(self.action_dim, size=(state.shape[0], 1))
        return action


class QNetDuel(QNetBase):  # Dueling DQN
    def __init__(self, net_dims: [int], state_dim: int, action_dim: int):
        super().__init__(state_dim=state_dim, action_dim=action_dim)
        self.net_state = build_mlp(dims=[state_dim, *net_dims])
        self.net_adv = build_mlp(dims=[net_dims[-1], 1])  # advantage value
        self.net_val = build_mlp(dims=[net_dims[-1], action_dim])  # Q value

        layer_init_with_orthogonal(self.net_adv[-1], std=0.1)
        layer_init_with_orthogonal(self.net_val[-1], std=0.1)

    def forward(self, state):
        state = self.state_norm(state)
        s_enc = self.net_state(state)  # encoded state
        q_val = self.net_val(s_enc)  # q value
        q_adv = self.net_adv(s_enc)  # advantage value
        value = q_val - q_val.mean(dim=1, keepdim=True) + q_adv  # dueling Q value
        return value

    def get_action(self, state):
        state = self.state_norm(state)
        if self.explore_rate < th.rand(1):
            s_enc = self.net_state(state)  # encoded state
            q_val = self.net_val(s_enc)  # q value
            action = q_val.argmax(dim=1, keepdim=True)
        else:
            action = th.randint(self.action_dim, size=(state.shape[0], 1))
        return action


class QNetTwin(QNetBase):  # Double DQN
    def __init__(self, dims: [int], state_dim: int, action_dim: int):
        super().__init__(state_dim=state_dim, action_dim=action_dim)
        self.net_state = build_mlp(dims=[state_dim, *dims])
        self.net_val1 = build_mlp(dims=[dims[-1], action_dim])  # Q value 1
        self.net_val2 = build_mlp(dims=[dims[-1], action_dim])  # Q value 2
        self.soft_max = nn.Softmax(dim=1)

        layer_init_with_orthogonal(self.net_val1[-1], std=0.1)
        layer_init_with_orthogonal(self.net_val2[-1], std=0.1)

    def forward(self, state):
        state = self.state_norm(state)
        s_enc = self.net_state(state)  # encoded state
        q_val = self.net_val1(s_enc)  # q value
        return q_val  # one group of Q values

    def get_q1_q2(self, state):
        state = self.state_norm(state)
        s_enc = self.net_state(state)  # encoded state
        q_val1 = self.net_val1(s_enc)  # q value 1
        q_val2 = self.net_val2(s_enc)  # q value 2
        return q_val1, q_val2  # two groups of Q values

    def get_action(self, state):
        state = self.state_norm(state)
        s_enc = self.net_state(state)  # encoded state
        q_val = self.net_val1(s_enc)  # q value
        if self.explore_rate < th.rand(1):
            action = q_val.argmax(dim=1, keepdim=True)
        else:
            a_prob = self.soft_max(q_val)
            action = th.multinomial(a_prob, num_samples=1)
        return action


class QNetTwinDuel(QNetBase):  # D3QN: Dueling Double DQN
    def __init__(self, dims: [int], state_dim: int, action_dim: int):
        super().__init__(state_dim=state_dim, action_dim=action_dim)
        self.net_state = build_mlp(dims=[state_dim, *dims])
        self.net_adv1 = build_mlp(dims=[dims[-1], 1])  # advantage value 1
        self.net_val1 = build_mlp(dims=[dims[-1], action_dim])  # Q value 1
        self.net_adv2 = build_mlp(dims=[dims[-1], 1])  # advantage value 2
        self.net_val2 = build_mlp(dims=[dims[-1], action_dim])  # Q value 2
        self.soft_max = nn.Softmax(dim=1)

        layer_init_with_orthogonal(self.net_adv1[-1], std=0.1)
        layer_init_with_orthogonal(self.net_val1[-1], std=0.1)
        layer_init_with_orthogonal(self.net_adv2[-1], std=0.1)
        layer_init_with_orthogonal(self.net_val2[-1], std=0.1)

    def forward(self, state):
        state = self.state_norm(state)
        s_enc = self.net_state(state)  # encoded state
        q_val = self.net_val1(s_enc)  # q value
        q_adv = self.net_adv1(s_enc)  # advantage value
        value = q_val - q_val.mean(dim=1, keepdim=True) + q_adv  # one dueling Q value
        return value

    def get_q1_q2(self, state):
        state = self.state_norm(state)
        s_enc = self.net_state(state)  # encoded state

        q_val1 = self.net_val1(s_enc)  # q value 1
        q_adv1 = self.net_adv1(s_enc)  # advantage value 1
        q_duel1 = q_val1 - q_val1.mean(dim=1, keepdim=True) + q_adv1

        q_val2 = self.net_val2(s_enc)  # q value 2
        q_adv2 = self.net_adv2(s_enc)  # advantage value 2
        q_duel2 = q_val2 - q_val2.mean(dim=1, keepdim=True) + q_adv2
        return q_duel1, q_duel2  # two dueling Q values

    def get_action(self, state):
        state = self.state_norm(state)
        s_enc = self.net_state(state)  # encoded state
        q_val = self.net_val1(s_enc)  # q value
        if self.explore_rate < th.rand(1):
            action = q_val.argmax(dim=1, keepdim=True)
        else:
            a_prob = self.soft_max(q_val)
            action = th.multinomial(a_prob, num_samples=1)
        return action
