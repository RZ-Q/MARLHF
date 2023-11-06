from typing import Any, Union, List
from collections import namedtuple
from easydict import EasyDict
import gym
import copy
import numpy as np

from overcooked_ai_py.mdp.actions import Action, Direction
from overcooked_ai_py.mdp.overcooked_mdp import PlayerState, OvercookedGridworld, OvercookedState, ObjectState, \
    SoupState, Recipe
from overcooked_ai_py.mdp.overcooked_env import OvercookedEnv, DEFAULT_ENV_PARAMS

# from utils import deep_merge_dicts
from envs.multiagentenv import MultiAgentEnv
from utils.dict2namedtuple import convert

OvercookEnvTimestep = namedtuple('OvercookEnvTimestep', ['obs', 'reward', 'done', 'info'])

# n, s = Direction.NORTH, Direction.SOUTH
# e, w = Direction.EAST, Direction.WEST
# stay, interact = Action.STAY, Action.INTERACT
# Action.ALL_ACTIONS: [n, s, e, w, stay, interact]


class OvercookEnv(MultiAgentEnv):
    config = EasyDict(
        dict(
            env_name="cramped_room",
            horizon=400,
            concat_obs=False,
            action_mask=True,
            shape_reward=True,
        )
    )

    def __init__(self, cfg, **kwargs):
        # Unpack arguments from sacred
        args = kwargs["env_args"]
        if isinstance(args, dict):
            args = convert(args)
        # self.args = deep_merge_dicts(self.config, cfg)
        self.args = args
        self.env_name = self.args.env_name
        self.horizon = self.args.horizon
        self.concat_obs = self.args.concat_obs
        self.action_mask = self.args.action_mask
        self.shape_reward = self.args.shape_reward
        self.mdp = OvercookedGridworld.from_layout_name(self.env_name)
        self.base_env = OvercookedEnv.from_mdp(self.mdp, horizon=self.horizon, info_level=0)

        # rightnow overcook environment encoding only support 2 agent game
        self.agent_num = 2
        self.action_dim = len(Action.ALL_ACTIONS)
        self.action_space = gym.spaces.Discrete(len(Action.ALL_ACTIONS))
        # set up obs shape
        featurize_fn = lambda mdp, state: mdp.lossless_state_encoding(state)
        self.featurize_fn = featurize_fn
        dummy_mdp = self.base_env.mdp
        dummy_state = dummy_mdp.get_standard_start_state()
        obs_shape = self.featurize_fn(dummy_mdp, dummy_state)[0].shape  # (5, 4, 26)
        obs_shape = (obs_shape[-1], *obs_shape[:-1])  # permute channel first
        if self.concat_obs:
            obs_shape = (obs_shape[0] * 2, *obs_shape[1:])
        else:
            obs_shape = (2, ) + obs_shape
        self.observation_space = gym.spaces.Box(low=0, high=1, shape=obs_shape, dtype=np.int64)
        if self.action_mask:
            self.observation_space = gym.spaces.Dict(
                {
                    'agent_state': self.observation_space,
                    'action_mask': gym.spaces.Box(
                        low=0, high=1, shape=(self.agent_num, self.action_dim), dtype=np.int64
                    )
                }
            )
        self.reward_space = gym.spaces.Box(low=0, high=100, shape=(1, ), dtype=np.float32)

    def seed(self, seed: int, dynamic_seed: bool = True) -> None:
        self._seed = seed
        self._dynamic_seed = dynamic_seed
        np.random.seed(self._seed)

    def close(self) -> None:
        # Note: the real env instance only has a empty close method, only pas
        pass

    def random_action(self):
        return [self.action_space.sample() for _ in range(self.agent_num)]

    def step(self, action):
        assert all(self.action_space.contains(a) for a in action), "%r (%s) invalid" % (action, type(action))
        agent_action, other_agent_action = [Action.INDEX_TO_ACTION[a] for a in action]

        if self.agent_idx == 0:
            joint_action = (agent_action, other_agent_action)
        else:
            joint_action = (other_agent_action, agent_action)

        next_state, reward, done, env_info = self.base_env.step(joint_action)
        reward = np.array([float(reward)])
        self._eval_episode_return += reward
        if self.shape_reward:
            self._eval_episode_return += sum(env_info['shaped_r_by_agent'])
            reward += sum(env_info['shaped_r_by_agent'])

        ob_p0, ob_p1 = self.featurize_fn(self.mdp, next_state)
        ob_p0, ob_p1 = self.obs_preprocess(ob_p0), self.obs_preprocess(ob_p1)
        if self.agent_idx == 0:
            both_agents_ob = [ob_p0, ob_p1]
        else:
            both_agents_ob = [ob_p1, ob_p0]
        if self.concat_obs:
            both_agents_ob = np.concatenate(both_agents_ob)
        else:
            both_agents_ob = np.stack(both_agents_ob)

        env_info["policy_agent_idx"] = self.agent_idx
        env_info["eval_episode_return"] = self._eval_episode_return
        env_info["other_agent_env_idx"] = 1 - self.agent_idx

        action_mask = self.getaction_mask()
        if self.action_mask:
            obs = {
                "agent_state": both_agents_ob,
                # "overcooked_state": self.base_env.state,
                "action_mask": action_mask
            }
        else:
            obs = both_agents_ob
        return OvercookEnvTimestep(obs, reward, done, env_info)

    def obs_preprocess(self, obs):
        obs = obs.transpose(2, 0, 1)
        return obs

    def reset(self):
        self.base_env.reset()
        self._eval_episode_return = 0
        self.mdp = self.base_env.mdp
        # random init agent index
        self.agent_idx = np.random.choice([0, 1])
        ob_p0, ob_p1 = self.featurize_fn(self.mdp, self.base_env.state)
        ob_p0, ob_p1 = self.obs_preprocess(ob_p0), self.obs_preprocess(ob_p1)

        if self.agent_idx == 0:
            both_agents_ob = [ob_p0, ob_p1]
        else:
            both_agents_ob = [ob_p1, ob_p0]
        if self.concat_obs:
            both_agents_ob = np.concatenate(both_agents_ob)
        else:
            both_agents_ob = np.stack(both_agents_ob)

        action_mask = self.getaction_mask()

        if self.action_mask:
            obs = {"agent_state": both_agents_ob, "action_mask": action_mask}
        else:
            obs = both_agents_ob
        return obs

    def get_available_actions(self):
        return self.mdp.get_actions(self.base_env.state)

    def getaction_mask(self):
        available_actions = self.get_available_actions()

        action_masks = np.zeros((self.agent_num, self.action_dim)).astype(np.int64)

        for i in range(self.action_dim):
            if Action.INDEX_TO_ACTION[i] in available_actions[0]:
                action_masks[0][i] = 1
            if Action.INDEX_TO_ACTION[i] in available_actions[1]:
                action_masks[1][i] = 1

        return action_masks

    def __repr__(self):
        return "DI-engine Overcooked Env"


class OvercookGameEnv():
    config = EasyDict(
        dict(
            env_name="cramped_room",
            horizon=400,
            concat_obs=False,
            action_mask=False,
            shape_reward=True,
        )
    )

    def __init__(self, cfg) -> None:
        self.args = deep_merge_dicts(self.config, cfg)
        self.env_name = self.args.env_name
        self.horizon = self.args.horizon
        self.concat_obs = self.args.concat_obs
        self.action_mask = self.args.action_mask
        self.shape_reward = self.args.shape_reward
        self.mdp = OvercookedGridworld.from_layout_name(self.env_name)
        self.base_env = OvercookedEnv.from_mdp(self.mdp, horizon=self.horizon, info_level=0)

        # rightnow overcook environment encoding only support 2 agent game
        self.agent_num = 2
        self.action_dim = len(Action.ALL_ACTIONS)
        self.action_space = gym.spaces.Discrete(len(Action.ALL_ACTIONS))
        # set up obs shape
        featurize_fn = lambda mdp, state: mdp.lossless_state_encoding(state)
        self.featurize_fn = featurize_fn
        dummy_mdp = self.base_env.mdp
        dummy_state = dummy_mdp.get_standard_start_state()
        obs_shape = self.featurize_fn(dummy_mdp, dummy_state)[0].shape  # (5, 4, 26)
        obs_shape = (obs_shape[-1], *obs_shape[:-1])  # permute channel first
        if self.concat_obs:
            obs_shape = (obs_shape[0] * 2, *obs_shape[1:])
        else:
            obs_shape = (2, ) + obs_shape
        self.observation_space = gym.spaces.Box(low=0, high=1, shape=obs_shape, dtype=np.int64)
        if self.action_mask:
            self.observation_space = gym.spaces.Dict(
                {
                    'agent_state': self.observation_space,
                    'action_mask': gym.spaces.Box(
                        low=0, high=1, shape=(self.agent_num, self.action_dim), dtype=np.int64
                    )
                }
            )

        self.reward_space = gym.spaces.Box(low=0, high=100, shape=(1, ), dtype=np.float32)

    def seed(self, seed: int, dynamic_seed: bool = True) -> None:
        self._seed = seed
        self._dynamic_seed = dynamic_seed
        np.random.seed(self._seed)

    def close(self) -> None:
        # Note: the real env instance only has a empty close method, only pass
        pass

    def random_action(self):
        return [self.action_space.sample() for _ in range(self.agent_num)]

    def step(self, action):
        assert all(self.action_space.contains(a) for a in action), "%r (%s) invalid" % (action, type(action))
        agent_action, other_agent_action = [Action.INDEX_TO_ACTION[a] for a in action]

        if self.agent_idx == 0:
            joint_action = (agent_action, other_agent_action)
        else:
            joint_action = (other_agent_action, agent_action)

        next_state, reward, done, env_info = self.base_env.step(joint_action)

        reward = np.array([float(reward)])
        self._eval_episode_return += reward
        if self.shape_reward:
            self._eval_episode_return += sum(env_info['shaped_r_by_agent'])
            reward += sum(env_info['shaped_r_by_agent'])
        ob_p0, ob_p1 = self.featurize_fn(self.mdp, next_state)
        ob_p0, ob_p1 = self.obs_preprocess(ob_p0), self.obs_preprocess(ob_p1)
        if self.agent_idx == 0:
            both_agents_ob = [ob_p0, ob_p1]
        else:
            both_agents_ob = [ob_p1, ob_p0]
        if self.concat_obs:
            both_agents_ob = np.concatenate(both_agents_ob)
        else:
            both_agents_ob = np.stack(both_agents_ob)

        env_info["policy_agent_idx"] = self.agent_idx
        env_info["eval_episode_return"] = self._eval_episode_return
        env_info["other_agent_env_idx"] = 1 - self.agent_idx

        action_mask = self.getaction_mask()
        if self.action_mask:
            obs = {"agent_state": both_agents_ob, "action_mask": action_mask}
        else:
            obs = both_agents_ob
        return OvercookEnvTimestep(obs, reward, done, env_info)

    def obs_preprocess(self, obs):
        obs = obs.transpose(2, 0, 1)
        return obs

    def reset(self):
        self.base_env.reset()
        self._eval_episode_return = 0
        self.mdp = self.base_env.mdp
        # random init agent index
        self.agent_idx = np.random.choice([0, 1])
        #fix init agent index
        self.agent_idx = 0
        ob_p0, ob_p1 = self.featurize_fn(self.mdp, self.base_env.state)
        ob_p0, ob_p1 = self.obs_preprocess(ob_p0), self.obs_preprocess(ob_p1)

        if self.agent_idx == 0:
            both_agents_ob = [ob_p0, ob_p1]
        else:
            both_agents_ob = [ob_p1, ob_p0]
        if self.concat_obs:
            both_agents_ob = np.concatenate(both_agents_ob)
        else:
            both_agents_ob = np.stack(both_agents_ob)

        action_mask = self.getaction_mask()

        if self.action_mask:
            obs = {"agent_state": both_agents_ob, "action_mask": action_mask}
        else:
            obs = both_agents_ob
        return obs

    def get_available_actions(self):
        return self.mdp.get_actions(self.base_env.state)

    def getaction_mask(self):
        available_actions = self.get_available_actions()

        action_masks = np.zeros((self.agent_num, self.action_dim)).astype(np.int64)

        for i in range(self.action_dim):
            if Action.INDEX_TO_ACTION[i] in available_actions[0]:
                action_masks[0][i] = 1
            if Action.INDEX_TO_ACTION[i] in available_actions[1]:
                action_masks[1][i] = 1

        return action_masks

    def __repr__(self):
        return "DI-engine Overcooked GameEnv"