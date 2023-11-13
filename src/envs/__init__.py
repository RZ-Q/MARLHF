from functools import partial
from smac.env import MultiAgentEnv
from .multiagentenv import MultiAgentEnv
from .starcraft2 import StarCraft2Env
import sys
import os

from .overcooked_env import OvercookEnv

def env_fn(env, **kwargs) -> MultiAgentEnv:
    return env(**kwargs)

REGISTRY = {}
REGISTRY["sc2"] = partial(env_fn, env=StarCraft2Env)

if sys.platform == "linux":
    os.environ.setdefault("SC2PATH",
                          os.path.join(os.getcwd(), "3rdparty", "StarCraftII"))
