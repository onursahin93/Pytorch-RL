"""
DDPG: Deep Deterministic Policy Gradient

Written by Guoqing Liu (v-liguoq@microsoft.com)

DDPG is a powerful actor-critic algorithm based on the determinstic policy gradient.

See these papers for details:
DPG:
http://proceedings.mlr.press/v32/silver14.pdf (David Silver et al., 2015)

DDPG:
https://arxiv.org/abs/1707.06347 (Timothy P. Lillicrap et al., 2016)

And, also these Github repo which was very helpful to me during this implementation:
https://github.com/sfujim/TD3

This implementation learns policies for continuous environments in the OpenAI Gym (https://gym.openai.com/).
Testing was focused on the MuJoCo control Suite.
"""

import os
import random
import argparse
import numpy as np
import scipy.signal
import gym
import torch
import torch.nn as nn
import torch.optim as optim

import sys
sys.path.append('..')
import utils.logger as logger
from datetime import datetime
from utils.scaler import Scaler
from models import Actor, Critic, DDPG
from replay_buffer import ReplayBuffer

# set device
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def parse_arguments():
    """ Parse Arguments from Commandline
    Return:
        args: commandline arguments (object)
    """
    parser = argparse.ArgumentParser(description=('Train policy on OpenAI Gym environment '
                                                  'using Proximal Policy Optimizer'))
    parser.add_argument('-e', '--env_name', type=str, default="HalfCheetah-v1",
                        help='OpenAI Gym environment name')
    parser.add_argument("--start_episodes", type=int, default=10,
                        help='How many episodes purely random policy is run for')
    parser.add_argument('-n', '--num_episodes', type=int, default=1000,
                        help='Number of episodes to run')
    parser.add_argument('-g', '--gamma', type=float, default=0.99,
                        help='Discount factor')
    parser.add_argument('-t', '--tau', type=float, default=0.005,
                        help='Target network update rate')
    parser.add_argument('-o', '--noise_std', type=float, default=0.1,
                        help='Std of Gaussian exploration noise')
    parser.add_argument('-b', '--batch_size', type=int, default=1,
                        help='Number of episodes per training batch')
    parser.add_argument('-f', '--eval_freq', type=int, default=10,
                        help='Number of training batch before test')
    parser.add_argument('-s', '--seed', type=int, default=0,
                        help='Random seed for all modules with randomness')
    args = parser.parse_args()
    return args


def set_global_seed(seed):
    """ Set Seeds of All Used Modules (Except Env) """
    np.random.seed(seed)
    random.seed(seed)
    torch.manual_seed(seed)


def configure_log_info(env_name, seed):
    """ Configure Log Information """
    cwd = os.path.join(os.getcwd(), 'log')
    now = datetime.utcnow().strftime("%b-%d_%H:%M:%S")  # create unique log file
    run_name = "{0}-{1}-{2}".format(env_name, seed, now)
    cwd = os.path.join(cwd, run_name)
    logger.configure(dir=cwd)


def run_episode(env, agent, replay_buffer, mode):
    """ Run single episode with option to animate """
    assert (mode in ["train", "test", "random"])
    obs = env.reset()
    done = False
    obs_step = 0.0
    episode_return = 0.0
    episode_length = 0
    while not done:
        obs = obs.astype(np.float32).flatten()
        obs = np.append(obs, obs_step)  # add time step feature
        current_obs = obs
        if mode == "train":
            action = agent.select_action(obs, True).flatten().astype(np.float32)
        elif mode == "test":
            action = agent.select_action(obs, False).flatten().astype(np.float32)
        else:
            action = env.action_space.sample().flatten().astype(np.float32)
        action = np.clip(action, -1.0, 1.0)
        obs, reward, done, _ = env.step(action)
        if not isinstance(reward, float):
            reward = np.asscalar(np.asarray(reward))
        episode_return += reward
        obs_step += 1e-3  # increment time step feature
        episode_length += 1
        done_bool = float(done)
        next_obs = obs.astype(np.float32).flatten()
        next_obs = np.append(next_obs, obs_step) 
        if mode != "test":
            replay_buffer.add(current_obs, action, next_obs, reward, done_bool)
    return episode_return, episode_length


def run_policy(env, agent, replay_buffer, mode="train", episodes=5):
    """ Rollout with agent and store trajectories """
    total_steps = 0
    returns = []
    for e in range(episodes):
        episode_return, episode_length = run_episode(env, agent, replay_buffer, mode)
        total_steps += episode_length
        returns.append(episode_return)
    return returns, total_steps


def train(env_name, start_episodes, num_episodes, gamma, tau, noise_std, batch_size, eval_freq, seed):
    """ Main training loop
    Args:
        env_name: OpenAI Gym environment name, e.g. 'Hopper-v1'
        start_episodes: how many episodes purely random policy is run for
        num_episodes: maximum number of episodes to run
        gamma: reward discount factor
        tau: target network update rate
        batch_size: number of episodes per policy training batch
        eval_freq: number of training batch before test
        seed: random seed for all modules with randomness
    """
    # set seeds
    set_global_seed(seed)
    # configure log
    configure_log_info(env_name, seed)

    # create env
    env = gym.make(env_name)
    env.seed(seed) # set env seed
    obs_dim = env.observation_space.shape[0]
    obs_dim += 1  # add 1 to obs dimension for time step feature (see run_episode())
    act_dim = env.action_space.shape[0]

    # create actor and target actor
    actor = Actor(obs_dim, act_dim, float(env.action_space.high[0])).to(device)
    target_actor = Actor(obs_dim, act_dim, float(env.action_space.high[0])).to(device)

    # create critic and target critic
    critic = Critic(obs_dim, act_dim).to(device)
    target_critic = Critic(obs_dim, act_dim).to(device)

    # create DDPG agent (hollowed object)
    agent = DDPG(actor, critic, target_actor, target_critic, noise_std, gamma, tau)
    agent.align_target()

    # create replay_buffer
    replay_buffer = ReplayBuffer()
    # run a few episodes of untrained policy to initialize scaler and fill in replay buffer
    run_policy(env, agent, replay_buffer, mode="random", episodes=start_episodes)
    
    num_iteration = num_episodes // eval_freq
    current_episodes = 0
    current_steps = 0
    for iter in range(num_iteration):
        # train models
        for i in range(eval_freq):
            # sample transitions
            train_returns, total_steps = run_policy(env, agent, replay_buffer, mode="train", episodes=batch_size)
            current_episodes += batch_size
            current_steps += total_steps
            logger.info('[train] average return:{0}, std return: {1}'.format(np.mean(train_returns), np.std(train_returns)))
            # train
            num_epoch = total_steps // batch_size
            for e in range(num_epoch):
                observation, action, reward, next_obs, done = replay_buffer.sample()
                agent.update(observation, action, reward, next_obs, done)
        # test models
        num_test_episodes = 10
        returns, _ = run_policy(env, agent, replay_buffer, mode="test", episodes=num_test_episodes)
        avg_return = np.mean(returns)
        std_return = np.std(returns)
        logger.record_tabular('iteration', iter)
        logger.record_tabular('episodes', current_episodes)
        logger.record_tabular('steps', current_steps)
        logger.record_tabular('avg_return', avg_return)
        logger.record_tabular('std_return', std_return)
        logger.dump_tabular()


def main():
    # parse arguments
    args = parse_arguments()
    # train loop
    train(**vars(args))


if __name__ == "__main__":
    main()






