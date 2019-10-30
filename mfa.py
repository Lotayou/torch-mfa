import numpy as np
import torch
from torch.distributions import multinomial
from torch.utils.data import Dataset, DataLoader
from torch.utils.data import RandomSampler, SequentialSampler, BatchSampler
import time
import math


class MFA(torch.nn.Module):
    def __init__(self, n_components, n_features, n_factors, isotropic=True, init_method='rnd_samples'):
        super(MFA, self).__init__()
        self.n_components = n_components
        self.n_features = n_features
        self.n_factors = n_factors
        self.isotropic = isotropic
        self.init_method = init_method
        sgd_support=False

        self.MU = torch.nn.Parameter(torch.zeros(n_components, n_features), requires_grad=sgd_support)
        self.A = torch.nn.Parameter(torch.zeros(n_components, n_features, n_factors), requires_grad=sgd_support)
        self.log_D = torch.nn.Parameter(torch.zeros(n_components, n_features), requires_grad=sgd_support)
        self.PI_logits = torch.nn.Parameter(torch.log(torch.ones(n_components)/float(n_components)),
                                            requires_grad=sgd_support)

    def sample(self, n, with_noise=True):
        K, d, l = self.A.shape
        c_nums = np.random.choice(K, n, p=torch.softmax(self.PI_logits, dim=0).detach().cpu().numpy())
        z_l = torch.randn(n, l, device=self.A.device)
        z_d = torch.randn(n, d, device=self.A.device) if with_noise else torch.zeros(n, d, device=self.A.device)
        samples = torch.stack([self.A[c_nums[i]] @ z_l[i] + self.MU[c_nums[i]] + z_d[i] * torch.exp(0.5*self.log_D[c_nums[i]])
                               for i in range(n)])
        return samples, c_nums

    def per_component_log_likelihood(self, x, sampled_features=None):
        if sampled_features is not None:
            return MFA._per_component_log_likelihood(x[:, sampled_features], torch.softmax(self.PI_logits, dim=0),
                                                     self.MU[:, sampled_features],
                                                     self.A[:, sampled_features],
                                                     self.log_D[:, sampled_features])
        return MFA._per_component_log_likelihood(x, torch.softmax(self.PI_logits, dim=0), self.MU, self.A, self.log_D)

    @staticmethod
    def _per_component_log_likelihood(x, PI, MU, A, log_D):
        K, d, l = A.shape
        AT = A.transpose(1, 2)
        iD = torch.exp(-log_D).view(K, d, 1)
        L = torch.eye(l, device=A.device).reshape(1, l, l) + AT @ (iD*A)
        iL = torch.inverse(L)

        def per_component_md(i):
            x_c = (x - MU[i].reshape(1, d)).T  # shape = (d, n)
            m_d_1 = (iD[i] * x_c) - ((iD[i] * A[i]) @ iL[i]) @ (AT[i] @ (iD[i] * x_c))
            return torch.sum(x_c * m_d_1, dim=0)

        m_d = torch.stack([per_component_md(i) for i in range(K)])
        det_L = torch.logdet(L)
        log_det_Sigma = det_L - torch.sum(torch.log(iD.reshape(K, d)), axis=1)
        log_prob_data_given_components = -0.5 * ((d*np.log(2.0*math.pi) + log_det_Sigma).reshape(K, 1) + m_d)
        return PI.reshape(1, K) + log_prob_data_given_components.T

    def log_prob(self, x, sampled_features=None):
        return torch.logsumexp(self.per_component_log_likelihood(x, sampled_features), dim=1)

    def log_likelihood(self, x, sampled_features=None):
        return torch.sum(self.log_prob(x, sampled_features))

    def log_responsibilities(self, x, sampled_features=None):
        comp_LLs = self.per_component_log_likelihood(x, sampled_features)
        return comp_LLs - torch.logsumexp(comp_LLs, dim=1).reshape(-1, 1)

    def responsibilities(self, x, sampled_features=None):
        return torch.exp(self.log_responsibilities(x, sampled_features))

    def map_component(self, x, sampled_features=None):
        """
        Get the Maximum a Posteriori component numbers
        """
        return torch.argmax(self.log_responsibilities(x, sampled_features), dim=1)

    def conditional_reconstruct(self, full_x, observed_features):
        """
        Calculates the mean of the conditional probability P(x_h | x_o)
        :param full_x: the full vectors (inclusing the hidden coordinates, which can contain any values)
        :param observed_features: tensor containing a list of the observed coordinates of x
        :return: A cloned version of full_x with the hidden features reconstructed

        References:
        https://www.math.uwaterloo.ca/~hwolkowi/matrixcookbook.pdf#subsubsection.8.1.3
        https://en.wikipedia.org/wiki/Woodbury_matrix_identity
        Note: This is equivalent to calling reconstruct with sampled_features
        """
        assert observed_features is not None
        K, d, l = self.A.shape
        c_i = self.map_component(full_x, observed_features)

        mask = torch.zeros(d, dtype=bool)
        mask[observed_features] = True

        A_a = self.A[c_i][:, ~mask, :]
        A_b = self.A[c_i][:, mask, :]
        MU_a = self.MU[c_i][:, ~mask]
        MU_b = self.MU[c_i][:, mask]
        iD_b = torch.exp(-self.log_D[c_i][:, mask]).unsqueeze(2)

        iL_b = torch.inverse(torch.eye(l, device=MU_b.device).reshape(1, l, l) + A_b.transpose(1, 2) @ (iD_b*A_b))
        x_b_l = ((A_b * iD_b).transpose(1,2) @ (full_x[:, mask] - MU_b).unsqueeze(2))
        x_hat = full_x.clone()
        x_hat[:, ~mask] =  (MU_a.unsqueeze(2) + A_a @ x_b_l - A_a @ (A_b.transpose(1, 2) @
                                                                     (iD_b * (A_b @ iL_b @ x_b_l)))).squeeze(dim=2)
        return x_hat

    def reconstruct(self, full_x, sampled_features=None):
        """
        Reconstruct samples from the model - find the MAP component and latent z for each sample and regenerate
        """
        K, d, l = self.A.shape
        c_i = self.map_component(full_x, sampled_features)

        used_features = sampled_features if sampled_features is not None else torch.arange(0, d)
        x = full_x[:, used_features]
        MU = self.MU[:, used_features]
        A = self.A[:, used_features]
        AT = A.transpose(1, 2)
        iD = torch.exp(-self.log_D[:, used_features]).unsqueeze(2)
        L = torch.eye(l, device=MU.device).reshape(1, l, l) + AT @ (iD*A)
        iL = torch.inverse(L)

        # per eq. 2 in Ghahramani and Hinton 1996 + the matrix inversion lemma (also described there).
        x_c = (x - MU[c_i]).unsqueeze(2)
        iD_c = iD[c_i]
        m_d_1 = (iD_c * x_c) - ((iD_c * A[c_i]) @ iL[c_i]) @ (AT[c_i] @ (iD_c * x_c))
        mu_z = AT[c_i] @ m_d_1
        # TODO: per Tipping and Bishop 1999, eq. 16, the optimal reconstruction is not the below.
        return (self.A[c_i] @ mu_z).reshape(-1, d) + self.MU[c_i]

    @staticmethod
    def _small_sample_ppca(x, n_factors):
        # See https://stats.stackexchange.com/questions/134282/relationship-between-svd-and-pca-how-to-use-svd-to-perform-pca
        mu = torch.mean(x, dim=0)
        # U, S, V = torch.svd(x - mu.reshape(1, -1))    # torch svd is less memory-efficient
        U, S, V = np.linalg.svd((x - mu.reshape(1, -1)).cpu().numpy(), full_matrices=False)
        V = torch.from_numpy(V.T).to(x.device)
        S = torch.from_numpy(S).to(x.device)
        sigma_squared = torch.sum(torch.pow(S[n_factors:], 2.0))/((x.shape[0]-1) * (x.shape[1]-n_factors))
        A = V[:, :n_factors] * torch.sqrt((torch.pow(S[:n_factors], 2.0).reshape(1, n_factors)/(x.shape[0]-1) - sigma_squared))
        return mu, A, torch.log(sigma_squared) * torch.ones(x.shape[1], device=x.device)

    def _init_from_data(self, x, samples_per_component):
        assert self.init_method == 'rnd_samples'

        K = self.n_components
        n = x.shape[0]
        l = self.n_factors
        m = samples_per_component
        o = np.random.choice(n, m*K, replace=False) if m*K < n else np.arange(n)
        assert n >= m*K
        params = [torch.stack(t) for t in zip(
            *[MFA._small_sample_ppca(x[o[i*m:(i+1)*m]], n_factors=l) for i in range(K)])]

        self.MU.data = params[0]
        self.A.data = params[1]
        self.log_D.data = params[2]

    def _parameters_sanity_check(self):
        K, d, l = self.A.shape
        assert torch.all(torch.softmax(self.PI_logits, dim=0) > 0.01/K), self.PI_logits
        assert torch.all(torch.exp(self.log_D) > 1e-5) and torch.all(torch.exp(self.log_D) < 1.0), \
            '{} - {}'.format(torch.min(self.log_D).item(), torch.max(self.log_D).item())
        assert torch.all(torch.abs(self.A) < 10.0), torch.max(torch.abs(self.A))
        assert torch.all(torch.abs(self.MU) < 1.0), torch.max(torch.abs(self.MU))

    def fit(self, x, max_iterations=20, responsibility_sampling=False):
        """
        Estimate Maximum Likelihood MPPCA parameters for the provided data using EM per
        Tipping, and Bishop. Mixtures of probabilistic principal component analyzers.
        :param x: training data (arranged in rows), shape = (<numbr of samples>, n_features)
        :param max_iterations: number of iterations
        :param responsibility_sampling: allows faster responsibility calculation by sampling data coordinates
        """
        assert not responsibility_sampling or type(responsibility_sampling) == float, 'set to desired sampling ratio'
        K, d, l = self.A.shape
        N = x.shape[0]

        print('Random init...')
        self._init_from_data(x, samples_per_component=(l+1)*2)
        print('Init log-likelihood =', round(torch.mean(self.log_prob(x)).item(), 1))

        def per_component_m_step(i):
            mu_i = torch.sum(r[:, [i]] * x, dim=0) / r_sum[i]
            s2_I = torch.exp(self.log_D[i, 0]) * torch.eye(l, device=x.device)
            inv_M_i = torch.inverse(self.A[i].T @ self.A[i] + s2_I)
            x_c = x - mu_i.reshape(1, d)
            SiAi = (1.0/r_sum[i]) * (r[:, [i]]*x_c).T @ (x_c @ self.A[i])
            invM_AT_Si_Ai = inv_M_i @ self.A[i].T @ SiAi
            A_i_new = SiAi @ torch.inverse(s2_I + invM_AT_Si_Ai)
            t1 = torch.trace(A_i_new.T @ (SiAi @ inv_M_i))
            trace_S_i = torch.sum(N/r_sum[i] * torch.mean(r[:, [i]]*x_c*x_c, dim=0))
            sigma_2_new = (trace_S_i - t1)/d
            return mu_i, A_i_new, torch.log(sigma_2_new) * torch.ones_like(self.log_D[i])

        for it in range(max_iterations):
            t = time.time()
            sampled_features = np.random.choice(d, int(d*responsibility_sampling)) if responsibility_sampling else None
            r = self.responsibilities(x, sampled_features=sampled_features)
            r_sum = torch.sum(r, dim=0)
            new_params = [torch.stack(t) for t in zip(*[per_component_m_step(i) for i in range(K)])]
            self.MU.data = new_params[0]
            self.A.data = new_params[1]
            self.log_D.data = new_params[2]
            self.PI_logits.data = torch.log(r_sum / torch.sum(r_sum))
            ll = round(torch.mean(self.log_prob(x)).item(), 1) if it % 5 == 0 else '.....'
            print('Iteration {}/{}, train log-likelihood = {}, took {:.1f} sec'.format(it, max_iterations, ll,
                                                                                   time.time()-t))

    def batch_fit(self, dataset: Dataset, batch_size=1000, test_size=1000, max_iterations=20, responsibility_sampling=False):
        """
        Estimate Maximum Likelihood MPPCA parameters for the provided data using EM per
        Tipping, and Bishop. Mixtures of probabilistic principal component analyzers.
        Memory-efficient batched implementation for large datasets that do not fit in memory:
        E step:
            For all mini-batches:
            - Calculate and store responsibilities
            - Accumulate sufficient statistics
        M step: 
            Re-calculate all parameters
        Note that incremental EM per Neal & Hinton, 1998 is not supported, since we can't maintain
            the full x x^T as sufficient statistic - we need to multiply by A to get a more compact
            representation.
        :param dataset: pytorch Dataset object containing the training data (will be iterated over)
        :param batch_size: the batch size
        :param max_iterations: number of iterations
        :param responsibility_sampling: allows faster responsibility calculation by sampling data coordinates
       """
        assert not responsibility_sampling or type(responsibility_sampling) == float, 'set to desired sampling ratio'
        K, d, l = self.A.shape

        print('Random init...')
        init_samples_per_component = (l+1)*2
        init_keys = [key for i, key in enumerate(RandomSampler(dataset)) if i < init_samples_per_component*K]
        init_samples, _ = zip(*[dataset[key] for key in init_keys])
        self._init_from_data(torch.stack(init_samples).to(self.MU.device), samples_per_component=init_samples_per_component)

        # Read some random samples for train-likelihood calculation
        # test_samples, _ = zip(*[dataset[key] for key in RandomSampler(dataset, num_samples=test_size, replacement=True)])
        all_keys = [key for key in SequentialSampler(dataset)]
        test_samples, _ = zip(*[dataset[key] for key in all_keys[:test_size]])
        test_samples = torch.stack(test_samples).to(self.MU.device)

        ll_log = []
        loader = DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=8)
        for it in range(max_iterations):
            t = time.time()

            # Sufficient statistics
            sum_r = torch.zeros(size=[K], dtype=torch.float64, device=self.MU.device)
            sum_r_x = torch.zeros(size=[K, d], dtype=torch.float64, device=self.MU.device)
            sum_r_x_x_A = torch.zeros(size=[K, d, l], dtype=torch.float64, device=self.MU.device)
            sum_r_norm_x = torch.zeros(K, dtype=torch.float64, device=self.MU.device)

            ll_log.append(torch.mean(self.log_prob(test_samples)).item())
            print('Iteration {}/{}, train log-likelihood={}:'.format(it, max_iterations, ll_log[-1]))

            for batch_x, _ in loader:
                print('E', end='', flush=True)
                batch_x = batch_x.to(self.MU.device)
                sampled_features = np.random.choice(d, int(d*responsibility_sampling)) if responsibility_sampling else None
                batch_r = self.responsibilities(batch_x, sampled_features=sampled_features)
                sum_r += torch.sum(batch_r, dim=0).double()
                sum_r_norm_x += torch.sum(batch_r * torch.sum(torch.pow(batch_x, 2.0), dim=1, keepdim=True), dim=0).double()
                for i in range(K):
                    batch_r_x = batch_r[:, [i]] * batch_x
                    sum_r_x[i] += torch.sum(batch_r_x, dim=0).double()
                    sum_r_x_x_A[i] += (batch_r_x.T @ (batch_x @ self.A[i])).double()

            print(' / M...', end='', flush=True)
            self.PI_logits.data = torch.log(sum_r / torch.sum(sum_r)).float()
            self.MU.data = (sum_r_x / sum_r.reshape(-1, 1)).float()
            SA = sum_r_x_x_A / sum_r.reshape(-1, 1, 1) - \
                 (self.MU.reshape(K, d, 1) @ (self.MU.reshape(K, 1, d) @ self.A)).double()
            s2_I = torch.exp(self.log_D[:, 0]).reshape(K, 1, 1) * torch.eye(l, device=self.MU.device).reshape(1, l, l)
            M = (self.A.transpose(1, 2) @ self.A + s2_I).double()
            inv_M = torch.stack([torch.inverse(M[i]) for i in range(K)])   # (K, l, l)
            invM_AT_S_A = inv_M @ self.A.double().transpose(1, 2) @ SA   # (K, l, l)
            self.A.data = torch.stack([(SA[i] @ torch.inverse(s2_I[i].double() + invM_AT_S_A[i])).float()
                                       for i in range(K)])
            t1 = torch.stack([torch.trace(self.A[i].double().T @ (SA[i] @ inv_M[i])) for i in range(K)])
            t_s = sum_r_norm_x / sum_r - torch.sum(torch.pow(self.MU, 2.0), dim=1).double()
            self.log_D.data = torch.log((t_s - t1)/d).float().reshape(-1, 1) * torch.ones_like(self.log_D)

            self._parameters_sanity_check()
            print(' ({} sec)'.format(time.time()-t))

        ll_log.append(torch.mean(self.log_prob(test_samples)).item())
        print('\nFinal train log-likelihood={}:'.format(ll_log[-1]))
        return ll_log

    def sgd_train(self, dataset: Dataset, batch_size=128, test_size=1000, max_epochs=10, responsibility_sampling=False):
        # self.PI_logits.requires_grad =
        self.MU.requires_grad = self.A.requires_grad = self.log_D.requires_grad = True
        K, d, l = self.A.shape

        all_keys = [key for key in SequentialSampler(dataset)]
        test_samples, _ = zip(*[dataset[key] for key in all_keys[:test_size]])
        test_samples = torch.stack(test_samples).to(self.MU.device)

        # optimizer = torch.optim.SGD(self.parameters(), lr=0.001, momentum=0.9)
        optimizer = torch.optim.Adam(self.parameters(), lr=0.001)
        loader = DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=8)
        ll_log = []
        self.train()
        for epoch in range(max_epochs):
            t = time.time()
            for idx, (batch_x, _) in enumerate(loader):
                print('.', end='', flush=True)
                if idx > 0 and idx%100 == 0:
                    print(torch.mean(self.log_prob(test_samples)).item())
                sampled_features = np.random.choice(d, int(d*responsibility_sampling)) if responsibility_sampling else None
                batch_x = batch_x.to(self.MU.device)
                optimizer.zero_grad()
                loss = -self.log_likelihood(batch_x, sampled_features=sampled_features) / batch_size
                loss.backward()
                optimizer.step()
            ll_log.append(torch.mean(self.log_prob(test_samples)).item())
            print('\nEpoch {}: Test ll = {} ({} sec)'.format(epoch, ll_log[-1], time.time()-t))
            self._parameters_sanity_check()
        self.PI_logits.requires_grad = self.MU.requires_grad = self.A.requires_grad = self.log_D.requires_grad = False
        return ll_log

    # def batch_incremental_fit(self, dataset: Dataset, batch_size=1000, max_iterations=20,
    #                           responsibility_sampling=False):
    #     """
    #     """
    #     K, d, l = self.A.shape
    #
    #     """
    #     Incremental implementation per "A view of the EM algorithm that justifies incremental,
    #     sparse, and other variants", Neal & Hinton, 1998.
    #     Adapted by Eitan for Mixture of Probabilistic PCA
    #     For all batches:
    #         - Calculate and store responsibilities
    #         - Update the sufficient statistics incrementally
    #         - Recalculate the parameters
    #
    #    """
    #     # Initial guess
    #     print('Random init...')
    #     init_samples_per_component = (l+1)*2
    #     init_keys = [key for i, key in enumerate(RandomSampler(dataset)) if i < init_samples_per_component*K]
    #     # init_keys = list(np.load('init_keys.npy'))
    #     init_samples, _ = zip(*[dataset[key] for key in init_keys])
    #     self._init_from_data(torch.stack(init_samples).cuda(), samples_per_component=init_samples_per_component)
    #
    #     all_keys = [key for key in SequentialSampler(dataset)]
    #     N = len(all_keys)
    #     # Read some random samples for train-likelihood calculation
    #     # test_samples, _ = zip(*[dataset[key] for key in RandomSampler(dataset, num_samples=batch_size, replacement=True)])
    #     test_samples, _ = zip(*[dataset[key] for key in all_keys[:batch_size*2]])
    #     test_samples = torch.stack(test_samples).cuda()
    #
    #     ll_log = []
    #     all_r = torch.zeros(size=[N, K], device=self.MU.device)
    #     sum_r = torch.zeros(size=[K], dtype=torch.float64, device=self.MU.device)
    #     sum_r_x = torch.zeros(size=[K, d], dtype=torch.float64, device=self.MU.device)
    #     sum_r_x_x = torch.zeros(size=[K, d, d], dtype=torch.float64, device=self.MU.device)
    #     sum_r_norm_x = torch.zeros(K, dtype=torch.float64, device=self.MU.device)
    #
    #     def update_parameters():
    #         self.PI.data = (sum_r / torch.sum(sum_r)).float()
    #         self.MU.data = (sum_r_x / sum_r.reshape(-1, 1)).float()
    #         SA = (sum_r_x_x / sum_r.reshape(-1, 1, 1) - (self.MU.reshape(K, d, 1) @ self.MU.reshape(K, 1, d)).double()) @ self.A.double()
    #         s2_I = torch.pow(self.D[:, 0], 2.0).reshape(K, 1, 1) * torch.eye(l, device=self.MU.device).reshape(1, l, l)
    #         inv_M = torch.inverse((self.A.transpose(1, 2) @ self.A + s2_I).double())   # (K, l, l)
    #         invM_AT_S_A = inv_M @ self.A.double().transpose(1, 2) @ SA   # (K, l, l)
    #         self.A.data = (SA @ torch.inverse(s2_I.double() + invM_AT_S_A)).float()      # (K, d, l)
    #         t1 = torch.stack([torch.trace(self.A[i].double().T @ (SA[i] @ inv_M[i])) for i in range(K)])
    #         t_s = sum_r_norm_x / sum_r - torch.sum(torch.pow(self.MU, 2.0), dim=1).double()
    #         self.D.data = torch.sqrt((t_s - t1)/d).float().reshape(-1, 1) * torch.ones_like(self.D)
    #
    #     for it in range(max_iterations):
    #
    #         ll_log.append(torch.mean(self.log_prob(test_samples)).item())
    #         print('Iteration {}/{}, train log-likelihood={}:'.format(it, max_iterations, ll_log[-1]))
    #
    #         for batch_keys in BatchSampler(RandomSampler(dataset), batch_size=batch_size, drop_last=False):
    #             batch_x, _ = zip(*[dataset[key] for key in batch_keys])
    #             batch_x = torch.stack(batch_x).cuda()
    #             sampled_features = np.random.choice(d, int(d*responsibility_sampling)) if responsibility_sampling else None
    #             print('E', end='', flush=True)
    #             batch_r = self.responsibilities(batch_x, sampled_features=sampled_features)
    #             batch_prev_r = all_r[batch_keys].clone()
    #
    #             # Update the sufficient statistics
    #             all_r[batch_keys] = batch_r
    #             for i in range(K):
    #                 r_diff = batch_r[:, [i]] - batch_prev_r[:, [i]]
    #                 sum_r[i] += torch.sum(r_diff).double()
    #                 sum_r_x[i] += torch.sum(r_diff * batch_x, dim=0).double()
    #                 sum_r_x_x[i] += ((r_diff * batch_x).T @ batch_x).double()
    #                 sum_r_norm_x[i] += torch.sum(r_diff * torch.pow(batch_x, 2.0)).double()
    #
    #             # if it > 0:
    #             update_parameters()
    #             print(torch.mean(self.log_prob(test_samples)).item())
    #         print()
    #         # if it == 0:
    #         #     update_parameters()
    #         #     print(torch.mean(self.log_prob(test_samples)).item())
    #
    #     ll_log.append(torch.mean(self.log_prob(test_samples)).item())
    #     print('\nFinal train log-likelihood={}:'.format(ll_log[-1]))
    #     return ll_log
    #
