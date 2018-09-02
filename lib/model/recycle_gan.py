from lib.model.spatial_translation import SpatialTranslationModel
from lib.model.temporal_predictor import TemporalPredictorModel
from lib.model.discriminator import Discriminator
from lib.buffer import ReplayBuffer
from lib.loss import GANLoss
from lib.utils import INFO

from torch.optim import Adam
import torch.nn.functional as F
import torch.nn as nn
import torch
import math

import itertools

"""
    This script define the structure of Re-cycle GAN
    Originally, we implement the whole computation under the tensor whose rank = 6
    However, this idea is memory-consuming.
    Also, we are inspired from the vid2vid
    Thus, the current implementation is under the condition with rank = 5

    On the other hand, since the future frame of 1st frame should be compute condition on the previous frames
    However, the previous frames of 1st frame are not existed
    Thus, the input tensor sequence should consider the previous frames also.
    We denote the length of sequence as T' = T + t - 1
"""

class ReCycleGAN(nn.Module):
    def __init__(self, A_channel = 3, B_channel = 3, T = 30, t = 2, r = 1, device = 'cpu'):
        """
            The constructor of Re-cycle GAN

            Arg:    A_channel   - The number of channel in domain A
                                  Default is 3
                    B_channel   - The number of channel in domain B
                                  Default is 3
                    T           - The total frame of video sequence in each training batch 
                                  (In vid2vid, T is 30)
                    t           - The tuple size
                                  Defulat is 2 in original paper
                    r           - The ratio to divide the channel in each module
                                  Default is 1 (without division)
                    device      - The device symbol you want to compute on
                                  Default is 'cpu'
        """
        super().__init__()

        # Store variables
        self.t = t
        self.T = T + self.t
        self.device = device

        # Define network object
        self.G_A_to_B = SpatialTranslationModel(n_in = A_channel, n_out = B_channel, r = r)
        self.G_B_to_A = SpatialTranslationModel(n_in = B_channel, n_out = A_channel, r = r)
        self.P_A = TemporalPredictorModel(n_in = self.t * A_channel, n_out = A_channel, r = r)
        self.P_B = TemporalPredictorModel(n_in = self.t * B_channel, n_out = B_channel, r = r)
        self.D_A = Discriminator(n_in = A_channel, r = r)
        self.D_B = Discriminator(n_in = B_channel, r = r)

        # Define loss and optimizer
        self.criterion_adv = GANLoss()
        self.criterion_l2 = nn.MSELoss()
        self.optim_G = Adam(itertools.chain(self.G_A_to_B.parameters(), self.G_B_to_A.parameters(), self.P_A.parameters(), self.P_B.parameters()), lr = 0.001)
        self.optim_D = Adam(itertools.chain(self.D_A.parameters(), self.D_B.parameters()), lr = 0.0001)

        # Define replay buffer (used in original CycleGAN)
        self.fake_a_buffer = ReplayBuffer()
        self.fake_b_buffer = ReplayBuffer()
        self.to(self.device)

    def setInput(self, true_a_seq, true_b_seq):
        """
            Set the input, and move to corresponding device

            Arg:    true_a_seq  - The torch.Tensor object in domain A, and the rank format is BT'CHW
                    true_b_seq  - The torch.Tensor object in domain B, and the rank format is BT'CHW
        """
        self.true_a_seq = true_a_seq.float().to(self.device)
        self.true_b_seq = true_b_seq.float().to(self.device)

    def forward(self, true_a = None, true_b = None, true_a_seq = None, true_b_seq = None, warning = True):
        """
            The usual forward process of the Re-cycleGAN
            There are 2 points you should notice:
                1.  This function can be only called during inference
                    You should call 'backward' function directly during training without calling this function
                2.  You should notice that the tensor should move to device previously!!!!!

            Arg:    true_a      - The current frame in domain A, default is None
                    true_b      - The current frame in domain B, default is None
                    true_a_seq  - The list object which contain the previous frames in domain A, default is None
                    true_b_seq  - The list object which contain the previous frames in domain B, default is None
                    warning     - Bool. You should set False if you are ensure that you call this function during purely inferencing
            Ret:    The dict object which contains the rendered images. Its format is: {
                        'true_a': The original frame in domain A
                        'fake_b': The rendered frame in domain B
                        'reco_a': The reconstructed frame in domain A
                        'true_b': The original frame in domain B
                        'fake_a': The rendered frame in domain A
                        'reco_b': The reconstructed frame in domain B
                    }
        """
        # Warn the user not to call this function during training
        fake_b_spat, fake_b_temp, fake_b, reco_a, fake_a_spat, fake_a_temp, fake_a, reco_b = None, None, None, None, None, None, None, None
        if warning:
            INFO("This function can be called during inference, you should call <backward> function to update the model!")

        # =====================================================================================================================
        # We consider as 3 cases:
        # 1. both domains are None, then raise exception
        # 2. domain A is not None, then render A -> B -> A
        # 3. domain B is not None, then render B -> A -> B
        # =====================================================================================================================
        if true_a is None and true_b is None and true_a_seq is None and true_b_seq is None:
            raise Exception("The input are all None. You should at least assign the input of one domain !")
        if true_a is not None and true_a_seq is not None:
            # Move to specific device
            true_a = true_a.to(self.device)
            true_a_seq = [frame.to(self.device) for frame in true_a_seq]

            # Get the tuple object before proceeding temporal predictor
            fake_b_tuple = []
            for i in range(self.t):
                fake_b_tuple.append(self.G_A_to_B(true_a_seq[i]))
            fake_b_tuple = torch.cat(fake_b_tuple, dim = 1)
            true_a_tuple = torch.cat(true_a_seq, dim = 1)
            true_a = true_a

            # Generate
            fake_b_spat = self.G_A_to_B(true_a)
            fake_b_temp = self.P_B(fake_b_tuple)
            fake_b = 0.5 * (fake_b_spat + fake_b_temp)
            reco_a = 0.5 * (self.G_B_to_A(self.P_B(fake_b_tuple)) + self.P_A(true_a_tuple))
        elif not (true_a is None and true_a_seq is None):
            raise Exception("true_a type: {} true_a_tuple type: {}. ".format(type(true_a), type(true_a_seq)), 
                "You should make sure to fill the both input if you want to utilize domain A!"
            )
        if true_b is not None and true_b_seq is not None:
            # Move to specific device
            true_b = true_b.to(self.device)
            true_b_seq = [frame.to(self.device) for frame in true_b_seq]

            # Get the tuple object before proceeding temporal predictor
            fake_a_tuple = []
            for i in range(self.t):
                fake_a_tuple.append(self.G_B_to_A(true_b_seq[i]))
            fake_a_tuple = torch.cat(fake_a_tuple, dim = 1)        
            true_b_tuple = torch.cat(true_b_seq, dim = 1)
            true_b = true_b

            # Generate
            fake_a_spat = self.G_B_to_A(true_b)
            fake_a_temp = self.P_A(fake_a_tuple)
            fake_a = 0.5 * (fake_a_spat + fake_a_temp)
            reco_b = 0.5 * (self.G_A_to_B(self.P_A(fake_a_tuple)) + self.P_B(true_b_tuple))
        elif not (true_b is None and true_b_seq is None):
            raise Exception("true_b type: {} true_b_tuple type: {}. ".format(type(true_b), type(true_b_seq)), 
                "You should make sure to fill the both input if you want to utilize domain B!"
            )
        return {
            'true_a': true_a,
            'fake_b_spat': fake_b_spat,
            'fake_b_temp': fake_b_temp,
            'fake_b': fake_b,
            'reco_a': reco_a,
            'true_b': true_b,
            'fake_a_spat': fake_a_spat,
            'fake_a_temp': fake_a_temp,
            'fake_a': fake_a,
            'reco_b': reco_b
        }

    def updateGenerator(self, true_x_tuple, fake_y_tuple, true_x_next, fake_y_next, P_X, P_Y, net_Y_to_X, net_D):
        """
            Update the generator and the temporal predictor for the given frame and tuples
            You should update the discriminator and obtain the fake prediction first

            Arg:    true_x_tuple    - The tuple tensor object in X domain, rank is [B, (t-1)*C, H, W]
                    fake_y_tuple    - The generated tuple tensor object in Y domain, rank is [B, (t-1)*C, H, W]
                    true_x_next     - The true future frame in domain X
                    fake_y_next     - The current generated frame in domain Y
                    P_X             - The temporal predictor of X domain
                    P_Y             - The temporal predictor of Y domain
                    net_G           - The generator (Y -> X)
                    net_D           - The discriminator of domain Y
        """
        fake_x_next = P_X(true_x_tuple)
        reco_x_next = net_Y_to_X(P_Y(fake_y_tuple))
        fake_pred = net_D(fake_y_next)
        loss_G = self.criterion_adv(fake_pred, True) + \
            self.criterion_l2(reco_x_next, true_x_next) * 10. + \
            self.criterion_l2(fake_x_next, true_x_next) * 10.
        return loss_G

    def updateDiscriminator(self, fake_frame, true_frame, net_D):
        """
            Update the discriminator for the given frame and discriminator

            Arg:    fake_frame      - The fake image in specific domain, and the rank format is BCHW
                    true_frame      - The true image in specific domain, and the rank format is BCHW
                    net_D           - The discriminator of specific domain
            Ret:    The fake prediction
        """
        fake_pred = net_D(fake_frame.detach())
        true_pred = net_D(true_frame)
        loss_D = self.criterion_adv(true_pred, True) + self.criterion_adv(fake_pred, False)
        return loss_D 

    def backward(self):
        """
            The backward process of Re-cycle GAN
            You can call this function directly during training
        """
        # Prepare input sequence (BTCHW -> T * BCHW)
        true_a_frame_list = [frame.squeeze(1) for frame in torch.chunk(self.true_a_seq, self.T, dim = 1)]
        true_b_frame_list = [frame.squeeze(1) for frame in torch.chunk(self.true_b_seq, self.T, dim = 1)]
        self.loss_D = 0.0
        self.loss_G = 0.0

        # Generate fake_tuple in opposite domain
        fake_b_frame_list = []
        fake_a_frame_list = []
        for i in range(self.T):
            fake_b_frame_list.append(self.G_A_to_B(true_a_frame_list[i]))
            fake_a_frame_list.append(self.G_B_to_A(true_b_frame_list[i]))

        # ==================== Accumulate loss by each time step ====================
        for i in range(self.t, self.T):

            # generator and predictor
            true_a_tuple = torch.cat(true_a_frame_list[i - self.t: i], dim = 1).detach()
            fake_b_tuple = torch.cat(fake_b_frame_list[i - self.t: i], dim = 1).detach()
            true_b_tuple = torch.cat(true_b_frame_list[i - self.t: i], dim = 1).detach()
            fake_a_tuple = torch.cat(fake_a_frame_list[i - self.t: i], dim = 1).detach()
            self.loss_G += self.updateGenerator(true_a_tuple, fake_b_tuple, true_a_frame_list[i], true_b_frame_list[i], self.P_A, self.P_B, self.G_B_to_A, self.D_B)
            self.loss_G += self.updateGenerator(true_b_tuple, fake_a_tuple, true_b_frame_list[i], true_a_frame_list[i], self.P_B, self.P_A, self.G_A_to_B, self.D_A)

            # discriminator
            alter_fake_b = self.fake_b_buffer.push_and_pop(fake_b_frame_list[i])
            alter_fake_a = self.fake_a_buffer.push_and_pop(fake_a_frame_list[i])            
            self.loss_D += self.updateDiscriminator(alter_fake_b, true_b_frame_list[i], self.D_B)
            self.loss_D += self.updateDiscriminator(alter_fake_a, true_a_frame_list[i], self.D_A)

        # ==================== Update ====================

        # generator and predictor
        self.optim_G.zero_grad()
        self.loss_G.backward()
        self.optim_G.step()
        
        # discriminator
        self.optim_D.zero_grad()
        self.loss_D.backward()
        self.optim_D.step()