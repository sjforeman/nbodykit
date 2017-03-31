from __future__ import absolute_import
# the future import is important. or in python 2.7 we try to 
# import this module itself. Due to the unfortnate name conflict!

import numpy

from .base import FileType
from ..extern.six import string_types

class BigFile(FileType):
    """
    A file object to handle the reading of columns of data from 
    a ``bigfile`` file. ``bigfile`` is the default format of 
    FastPM and MP-Gadget.

    https://github.com/rainwoodman/bigfile
    """

    def __init__(self, path, exclude=None, header='.', dataset='./'):
        if not dataset.endswith('/'): dataset = dataset + '/'

        import bigfile
        if exclude is None:
            exclude = [header]

        self.dataset = dataset
        self.path = path

        # store the attributes
        self.attrs = {}

        # the file path
        with bigfile.BigFile(filename=path) as ff:
            columns = ff[self.dataset].blocks
            columns = list(set(columns) - set(exclude))

            ds = bigfile.BigData(ff[self.dataset], columns)

            # set the data type and size
            self.dtype = ds.dtype
            self.size = ds.size

            header = ff[header]

            attrs = header.attrs
            for k in attrs.keys():
                self.attrs[k] = numpy.array(attrs[k], copy=True)

    def read(self, columns, start, stop, step=1):
        """
        Read the specified column(s) over the given range, 
        as a dictionary

        'start' and 'stop' should be between 0 and :attr:`size`,
        which is the total size of the binary file (in particles)
        """ 
        import bigfile
        if isinstance(columns, string_types): columns = [columns]

        with bigfile.BigFile(filename=self.path)[self.dataset] as f:
            ds = bigfile.BigData(f, columns)
            return ds[start:stop][::step]
