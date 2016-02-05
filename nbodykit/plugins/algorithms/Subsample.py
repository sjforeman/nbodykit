from nbodykit.extensionpoints import Algorithm
import logging
import numpy

# for output
import h5py
import bigfile
import mpsort

from mpi4py import MPI

import nbodykit
from pmesh.particlemesh import ParticleMesh
from nbodykit.extensionpoints import DataSource, Painter


class Subsample(Algorithm):
    plugin_name = "Subsample"

    @classmethod
    def register(kls):
        from nbodykit.extensionpoints import DataSource
        p = kls.parser
        p.add_argument("datasource", type=DataSource.fromstring, 
                help="--list-datasource for help")
        p.add_argument("Nmesh", type=int,
                help='Size of FFT mesh for painting')
        p.add_argument("--seed", type=int, default=12345,
                help='seed')
        p.add_argument("--ratio", type=float, default=0.01,
                help='fraction of particles to keep')
        p.add_argument("--smoothing", type=float, default=None,
                help='Smoothing Length in distance units. '
                      'It has to be greater than the mesh resolution. '
                      'Otherwise the code will die. Default is the mesh resolution.')
        # this is for output..
        p.add_argument("--format", choices=['hdf5', 'mwhite'], default='hdf5', 
                help='format of the output')


    def run(self):
        comm = MPI.COMM_WORLD
        pm = ParticleMesh(self.datasource.BoxSize, self.Nmesh, dtype='f4', comm=None)
        if self.smoothing is None:
            self.smoothing = self.datasource.BoxSize[0] / self.Nmesh
        elif (self.datasource.BoxSize / self.Nmesh > self.smoothing).any():
            raise ValueError("smoothing is too small")
     
        painter = Painter.fromstring("DefaultPainter")
        painter.paint(pm, self.datasource)
        pm.r2c()
        def Smoothing(pm, complex):
            k = pm.k
            k2 = 0
            for ki in k:
                ki2 = ki ** 2
                complex *= numpy.exp(-0.5 * ki2 * self.smoothing ** 2)

        def NormalizeDC(pm, complex):
            """ removes the DC amplitude. This effectively
                divides by the mean
            """
            w = pm.w
            comm = pm.comm
            ind = []
            value = 0.0
            found = True
            for wi in w:
                if (wi != 0).all():
                    found = False
                    break
                ind.append((wi == 0).nonzero()[0][0])
            if found:
                ind = tuple(ind)
                value = numpy.abs(complex[ind])
            value = comm.allreduce(value, MPI.SUM)
            complex[:] /= value
            
        pm.transfer([Smoothing, NormalizeDC])
        pm.c2r()
        columns = ['Position', 'ID', 'Velocity']
        rng = numpy.random.RandomState(self.Nmesh)
        seedtable = rng.randint(1024*1024*1024, size=comm.size)
        rngtable = [numpy.random.RandomState(seed) for seed in seedtable]

        dtype = numpy.dtype([
                ('Position', ('f4', 3)),
                ('Velocity', ('f4', 3)),
                ('ID', 'u8'),
                ('Density', 'f4'),
                ]) 

        subsample = [numpy.empty(0, dtype=dtype)]
        stat = {}
        for Position, ID, Velocity in self.datasource.read(columns, stat):
            u = rngtable[comm.rank].uniform(size=len(ID))
            keep = u < self.ratio
            Nkeep = keep.sum()
            if Nkeep == 0: continue 
            data = numpy.empty(Nkeep, dtype=dtype)
            data['Position'][:] = Position[keep]
            data['Velocity'][:] = Velocity[keep]       
            data['Position'][:] /= self.datasource.BoxSize
            data['Velocity'][:] /= self.datasource.BoxSize
            data['ID'][:] = ID[keep] 

            layout = pm.decompose(data['Position'])
            pos1 = layout.exchange(data['Position'])
            density1 = pm.readout(pos1)
            density = layout.gather(density1)

            data['Density'][:] = density
            subsample.append(data)
             
        subsample = numpy.concatenate(subsample)
        mpsort.sort(subsample, orderby='ID')
        return subsample

    def save(self, output, data):
        if self.format == 'mwhite':
            self.write_mwhite_subsample(data, output)
        else:
            self.write_hdf5(data, output)

    def write_hdf5(self, subsample, output):

        size = self.comm.allreduce(len(subsample))
        offset = sum(self.comm.allgather(len(subsample))[:self.comm.rank])

        if self.comm.rank == 0:
            with h5py.File(output, 'w') as ff:
                dataset = ff.create_dataset(name='Subsample',
                        dtype=subsample.dtype, shape=(size,))
                dataset.attrs['Ratio'] = self.ratio
                dataset.attrs['CommSize'] = self.comm.size 
                dataset.attrs['Seed'] = self.seed
                dataset.attrs['Smoothing'] = self.smoothing
                dataset.attrs['Nmesh'] = self.Nmesh
                dataset.attrs['Original'] = self.datasource.string
                dataset.attrs['BoxSize'] = self.datasource.BoxSize

        for i in range(self.comm.size):
            self.comm.barrier()
            if i != self.comm.rank: continue
                 
            with h5py.File(output, 'r+') as ff:
                dataset = ff['Subsample']
                dataset[offset:len(subsample) + offset] = subsample

    def write_mwhite_subsample(self, subsample, output):
        size = self.comm.allreduce(len(subsample))
        offset = sum(self.comm.allgather(len(subsample))[:self.comm.rank])

        if self.comm.rank == 0:
            with open(output, 'wb') as ff:
                dtype = numpy.dtype([
                        ('eflag', 'int32'),
                        ('hsize', 'int32'),
                        ('npart', 'int32'),
                         ('nsph', 'int32'),
                         ('nstar', 'int32'),
                         ('aa', 'float'),
                         ('gravsmooth', 'float')])
                header = numpy.zeros((), dtype=dtype)
                header['eflag'] = 1
                header['hsize'] = 20
                header['npart'] = size
                header.tofile(ff)

        self.comm.barrier()

        with open(output, 'r+b') as ff:
            ff.seek(28 + offset * 12)
            numpy.float32(subsample['Position']).tofile(ff)
            ff.seek(28 + offset * 12 + size * 12)
            numpy.float32(subsample['Velocity']).tofile(ff)
            ff.seek(28 + offset * 4 + size * 24)
            numpy.float32(subsample['Density']).tofile(ff)
            ff.seek(28 + offset * 8 + size * 28)
            numpy.uint64(subsample['ID']).tofile(ff)