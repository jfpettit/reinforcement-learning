# import needed packages
import numpy as np
import gym
import torch
from flare.kindling import utils
from flare.polgrad import BasePolicyGradient
import flare.kindling as fk
import torch.nn.functional as F
from flare.kindling.mpi_pytorch import mpi_avg_grads, mpi_avg


class A2C(BasePolicyGradient):
    r"""
    An implementation of the Advantage Actor Critic (A2C) reinforcement learning algorithm.

    Args:
        env_fn: lambda function making the desired gym environment.
            Example::

                import gym
                env_fn = lambda: gym.make("CartPole-v1")
                agent = PPO(env_fn)
        hidden_sizes: Tuple of integers representing hidden layer sizes for the MLP policy.
        actorcritic: Class for policy and value networks.
        gamma: Discount factor for GAE-lambda estimation.
        lam: Lambda for GAE-lambda estimation.
        steps_per_epoch: Number of state, action, reward, done tuples to train on per epoch.
        pol_lr: Learning rate for the policy optimizer.
        val_lr: Learning rate for the value optimizer.
        seed: random seeding for NumPy and PyTorch.
        state_preproc: An optional state preprocessing function. Any desired manipulations to the state before it is passed to the agent can be performed here. The state_preproc function must take in and return a NumPy array.
            Example::

                def state_square(state):
                    state = state**2
                    return state
                agent = PPO(env_fn, state_preproc=state_square, state_sze=shape_of_state_after_preprocessing)
        state_sze: If a state preprocessing function is included, the size of the state after preprocessing must be passed in as well.
        logger_dir: Directory to log results to.
        tensorboard: Whether or not to use tensorboard logging.
        save_screen: Whether to save rendered screen images to a pickled file. Saves within logger_dir.
        save_states: Whether to save environment states to a pickled file. Saves within logger_dir.
    """
    def __init__(
        self,
        env,
        hidden_sizes=(64, 32),
        actorcritic=fk.FireActorCritic,
        gamma=0.99,
        lam=0.97,
        steps_per_epoch=4000,
        pol_lr=3e-4,
        val_lr=1e-3,
        seed=0,
        state_preproc=None,
        state_sze=None,
        logger_dir=None,
        tensorboard=True,
        save_screen=False,
        save_states=False,
    ):
        super().__init__(
            env,
            actorcritic=actorcritic,
            gamma=gamma,
            lam=lam,
            steps_per_epoch=steps_per_epoch,
            hidden_sizes=hidden_sizes,
            seed=seed,
            state_sze=state_sze,
            state_preproc=state_preproc,
            logger_dir=logger_dir,
            tensorboard=tensorboard,
            save_screen=save_screen,
            save_states=save_states,
        )

        self.policy_optimizer = torch.optim.Adam(self.ac.policy.parameters(), lr=pol_lr)
        self.value_optimizer = torch.optim.Adam(self.ac.value_f.parameters(), lr=val_lr)

        self.maxkl = 0.01

    def get_name(self):
        return self.__class__.__name__

    def calc_pol_loss(self, logps, advs):
        return -(logps*advs).mean()

    def calc_val_loss(self, vals, rets):
        return ((vals - rets)**2).mean()

    def update(self):
        """Update rule for Advantage Actor Critic algorithm."""
        states, acts, advs, rets, logprobs_old = [
            torch.as_tensor(x, dtype=torch.float32) for x in self.buffer.get()
        ]
        values = self.ac.value_f(states)
        val_loss_old = self.calc_val_loss(values, rets)

        _, logp, _ = self.ac.policy(states, acts)
        approx_ent = (-logp).mean()
        pol_loss_old = self.calc_pol_loss(logp, advs) 

        self.policy_optimizer.zero_grad()
        _, logp, _ = self.ac.policy(states, acts)
        kl = mpi_avg((logprobs_old - logp).mean().item())
        if kl > 1.5 * self.maxkl:
            self.logger.log(f"Warning: policy update hit max KL.")
        pol_loss = self.calc_pol_loss(logp, advs) 
        pol_loss.backward()
        mpi_avg_grads(self.ac.policy)
        self.policy_optimizer.step()

        for _ in range(80):
            self.value_optimizer.zero_grad()
            values = self.ac.value_f(states)
            val_loss = self.calc_val_loss(values, rets)
            val_loss.backward()
            mpi_avg_grads(self.ac.value_f)
            self.value_optimizer.step()

        self.logger.store(
            PolicyLoss=pol_loss_old.detach().numpy(),
            ValueLoss=val_loss_old.detach().numpy(),
            KL=kl,
            Entropy=approx_ent.detach().numpy(),
            DeltaPolLoss=(pol_loss - pol_loss_old).detach().numpy(),
            DeltaValLoss=(val_loss - val_loss_old).detach().numpy(),
        )
        return pol_loss, val_loss, approx_ent, kl