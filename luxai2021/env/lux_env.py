"""
Implements the base class for a Lux environment
"""
import traceback
import gym
import os
import copy
import glob
import random  
import numpy as np
from stable_baselines3.common.callbacks import BaseCallback

from ..game.game import Game
from ..game.match_controller import GameStepFailedException, MatchController
from ..game.constants import Constants


class SaveReplayAndModelCallback(BaseCallback):
    """
    Callback for saving a replay of a model every ``save_freq`` calls
    to ``env.step()``.

    .. warning::

      When using multiple environments, each call to  ``env.step()``
      will effectively correspond to ``n_envs`` steps.
      To account for that, you can use ``save_freq = max(save_freq // n_envs, 1)``

    :param save_freq:
    :param save_path: Path to the folder where the model will be saved.
    :param name_prefix: Common prefix to the saved models
    :param verbose:
    """

    def __init__(self, save_freq: int, save_path: str, replay_env, replay_num_episodes=5, name_prefix: str = "rl_model", verbose: int = 0):
        super(SaveReplayAndModelCallback, self).__init__(verbose)
        self.save_freq = save_freq
        self.save_path = save_path
        self.name_prefix = name_prefix
        self.replay_env = replay_env
        self.replay_num_episodes = replay_num_episodes
        print(f"Logging models and replays to '{self.save_path}'.")

    def _init_callback(self) -> None:
        # Create folder if needed
        if self.save_path is not None:
            os.makedirs(self.save_path, exist_ok=True)

    def _on_step(self) -> bool:
        if self.n_calls % self.save_freq == 0:
            # Save the model
            path = os.path.join(self.save_path, f"{self.name_prefix}_step{self.num_timesteps}")
            self.model.save(path)
            
            # Run a bunch of games to creates replays using the replay environment
            for i in range(self.replay_num_episodes):
                self.replay_env.game.configs["seed"] = i
                self.replay_env.set_replay_path(self.save_path, f"{self.name_prefix}_step{self.num_timesteps}_seed{i}")

                try:
                    self.replay_env.reset() # Runs  a whole game because no training agent is attached
                except StopIteration:
                    # Game finished successfully
                    pass
                except Exception as e:
                    # Failure
                    print("Replay environment failed.")
                    print(repr(e))
                    print(''.join(traceback.format_exception(None, e, e.__traceback__)))
                    pass
                
            
            if self.verbose > 1:
                print(f"Saved model checkpoint and replay to {path}")
        return True


class LuxEnvironment(gym.Env):
    """
    Custom Environment that follows gym interface
    """
    metadata = {'render.modes': ['human']}

    def __init__(
        self, 
        configs, 
        learning_agent, 
        opponent_agents, 
        initial_opponent_policy="self-play", 
        model_update_step_freq=None,
        model_save_path=None,  
        replay_validate=None, 
        replay_folder=None, 
        replay_prefix="replay"
        ):
        """
        THe initializer
        :param configs:
        :param learning_agent:
        :param opponent_agent:
        """
        super(LuxEnvironment, self).__init__()

        # Create the game
        self.game = Game(configs)

        if initial_opponent_policy in opponent_agents.keys():
            self.opponent_policy = initial_opponent_policy
        else:
            self.opponent_policy = list(opponent_agents.keys())[0]
        print(f"Initial opponent policy: {self.opponent_policy}")
    
        opponent_agent = opponent_agents[self.opponent_policy]
        self.match_controller = MatchController(self.game, 
                                                agents=[learning_agent, opponent_agent], 
                                                replay_validate=replay_validate)
        
        self.replay_prefix = replay_prefix
        self.replay_folder = replay_folder
        self.model_update_step_freq = model_update_step_freq
        self.model_save_path = model_save_path 

        self.action_space = []
        if hasattr( learning_agent, 'action_space' ):
            self.action_space = learning_agent.action_space
        
        self.observation_space = {}
        if hasattr( learning_agent, 'observation_space' ):
            self.observation_space = learning_agent.observation_space

        self.learning_agent = learning_agent
        self.opponent_agents = opponent_agents
        self.opponent_agent = opponent_agent

        self.current_step = 0
        self.total_env_step = 0 
        self.match_generator = None

        self.last_observation_object = None
        self.num_switch = 0
        self.last_unit_obs = None
        
    def set_replay_path(self, replay_folder, replay_prefix):
        """
        Override the replay prefix

        Args:
            replay_prefix ([type]):
        """
        self.replay_prefix = replay_prefix
        self.replay_folder = replay_folder

    def step(self, action_code):
        """
        Take this action, then get the state at the next action
        :param action_code:
        :return:
        """
        # Decision for 1 unit or city
        self.learning_agent.take_action(action_code,
                                        self.game,
                                        unit=self.last_observation_object[0],
                                        city_tile=self.last_observation_object[1],
                                        team=self.last_observation_object[2]
                                        )

        self.current_step += 1
        self.total_env_step += 1

        # Get the next observation
        is_new_turn = True
        is_game_over = False
        is_game_error = False
        try:
            (unit, city_tile, team, is_new_turn) = next(self.match_generator)
            if hasattr(self.learning_agent, "get_base_observation"):
                base_obs = self.learning_agent.get_base_observation(self.game,  team, self.last_unit_obs)
                obs = self.learning_agent.get_observation(self.game, unit, city_tile, team, is_new_turn, base_obs)
            else:
                obs = self.learning_agent.get_observation(self.game, unit, None, team)

            # self.learning_agent.get_last_observation(base_obs)
            self.last_observation_object = (unit, city_tile, team, is_new_turn)
        except StopIteration:
            # The game episode is done.
            is_game_over = True
            obs = None
        except GameStepFailedException:
            # Game step failed, assign a game lost reward to not incentivise this
            is_game_over = True
            obs = None
            is_game_error = True

        # Calculate reward for this step
        reward = self.learning_agent.get_reward(self.game, is_game_over, is_new_turn, is_game_error)

        if self.model_update_step_freq != None:
            # switch opponent policy in training
            if (len(self.opponent_agents) > 1)&(self.total_env_step % self.model_update_step_freq == 0):
                self.switch_opponent_policy()
            
        return obs, reward, is_game_over, {}  # self.learning_agent.rewards

    def reset(self):
        """

        :return:
        """
        self.current_step = 0
        self.last_observation_object = None

        # Reset game + map
        self.match_controller.reset()
        if self.replay_folder:
            # Tell the game to log replays
            self.game.start_replay_logging(stateful=True, replay_folder=self.replay_folder, replay_filename_prefix=self.replay_prefix)

        self.match_generator = self.match_controller.run_to_next_observation()
        (unit, city_tile, team, is_new_turn) = next(self.match_generator)

        if hasattr(self.learning_agent, "get_base_observation"):
            base_obs = self.learning_agent.get_base_observation(self.game,  team, self.last_unit_obs)
            obs = self.learning_agent.get_observation(self.game, unit, city_tile, team, is_new_turn, base_obs)
        else:  # for Unet
            obs = self.learning_agent.get_observation(self.game, unit, city_tile, team)
        self.last_observation_object = (unit, city_tile, team, is_new_turn)
        return obs

    def render(self, **kwargs):
        """

        :param kwargs:
        :return:
        """
        print(self.current_step)
        print(self.game.map.get_map_string())

    def run_no_learn(self):
        """
        Steps until the environment is "done".
        Both agents have to be in inference mode
        """

        for agent in self.match_controller.agents:
            assert agent.get_agent_type() == Constants.AGENT_TYPE.AGENT, "Both agents must be in inference mode"

        self.current_step = 0
        self.last_observation_object = None

        # Reset game + map
        self.match_controller.reset(randomize_team_order=False)
        # Running
        self.match_generator = self.match_controller.run_to_next_observation()
        try:
            next(self.match_generator)
        except StopIteration:
            # The game episode is done.
            is_game_error = False
            print('Episode run finished successfully!')
        except GameStepFailedException:
            # Game step failed.
            is_game_error = True

        return is_game_error
    
    def opponent_model_update(self):
        # for self-play
        if self.opponent_policy == 'self-play':
            # models = glob.glob(str(self.model_save_path)+'/rl_cnn_model_*_steps.onnx')
            models = glob.glob(str(self.model_save_path)+'/rl_cnn_model_*_steps.pth')
            p = random.random()
            if p < 0.5:  # sampling old model
                pretrained_model_path = random.choice(models)
            else:  # latest model
                pretrained_model_path = sorted(models, key=lambda x: int(x.split('_')[-2]), reverse=True)[0]
            self.opponent_agent.set_model(pretrained_model_path)

        # for imitation agent
        elif self.opponent_policy == 'imitation':
            self.opponent_agent.set_model()

    def is_valid_opponent_model_update(self):
        # return len(glob.glob(str(self.model_save_path)+'/rl_cnn_model_*_steps.onnx')) > 0
        return len(glob.glob(str(self.model_save_path)+'/rl_cnn_model_*_steps.pth')) > 0
    
    def switch_opponent_policy(self):
        current_opponent_policy = self.opponent_policy
        new_opponent_policy = self.opponent_policy
        self.num_switch += 1
        p = random.random()
        # self-play modelを読み込むとkilledになる
        if (p < 0.5)&("self-play" in self.opponent_agents.keys())&(self.is_valid_opponent_model_update()):
            new_opponent_policy = "self-play"
        elif (0.5 <= p)&("imitation" in self.opponent_agents.keys()):
            new_opponent_policy = "imitation" 

        if current_opponent_policy != new_opponent_policy:
            self.opponent_policy = new_opponent_policy
            self.opponent_agent = self.opponent_agents[self.opponent_policy]
            self.opponent_model_update()
            print(f"[STEP: {self.total_env_step}] Switch opponent agent: {current_opponent_policy} -> {self.opponent_policy}")
