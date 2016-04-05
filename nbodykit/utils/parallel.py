from mpi4py import MPI
import os
import traceback
import logging

from nbodykit.extensionpoints import set_nbkit_comm

def split_ranks(N_ranks, N, include_all=False):
    """
    Divide the ranks into chunks, attempting to have `N` ranks
    in each chunk. This removes the master (0) rank, such 
    that `N_ranks - 1` ranks are available to be grouped
    
    Parameters
    ----------
    N_ranks : int
        the total number of ranks available
    N : int
        the desired number of ranks per worker
    include_all : bool, optional
        if `True`, then do not force each group to have 
        exactly `N` ranks, instead including the remainder as well;
        default is `False`
    """
    available = list(range(1, N_ranks)) # available ranks to do work
    total = len(available)
    extra_ranks = total % N
    
    start = 0
    stop = N  
    for i in range(total//N):
        
        ranks = available[start:stop]
        start = stop
        stop += N
        if include_all and extra_ranks:
            stop += 1
            extra_ranks -= 1
            
        yield i, ranks
       
def enum(*sequential, **named):
    enums = dict(zip(sequential, range(len(sequential))), **named)
    return type('Enum', (), enums)
    

class MPIPool(object):
    """
    A pool that distributes tasks over a set of MPI processes, using
    a specified number of independent workers.
    
    Given the specified number of independent workers (which compute
    tasks in parallel), the total number of available CPUs will be 
    divided evenly.
    
    The return value of the task function is ignored, since it is
    designed to run an nbodykit `Algorithm`, which outputs
    the relevant to 
    """
    logger = logging.getLogger('MPIPool')
    
    def __init__(self, task_function, 
                       cpus_per_worker, 
                       comm=None, 
                       debug=False, 
                       use_all_cpus=False):
        """
        Parameters
        ----------
        task_function : callable
            the task function which takes the `task` value its only argument
        cpus_per_worker : int
            the desired number of ranks assigned to each independent
            worker in the pool
        comm : MPI communicator, optional
            the global communicator that will be split so each worker
            has a subset of CPUs available; default is COMM_WORLD
        debug : bool, optional
            if `True`, set the logging level to `DEBUG`, which prints 
            out much more information; default is `False`
        use_all_cpus : bool, optional
            if `True`, use all available CPUs, including the remainder
            if `cpus_per_worker` is not divide the total number of CPUs
            evenly; default is `False`
        """
        if debug:
            self.logger.setLevel(logging.DEBUG)
            
        self.function        = task_function
        self.cpus_per_worker = cpus_per_worker
        self.use_all_cpus    = use_all_cpus
        
        # the main communicator
        self.comm      = MPI.COMM_WORLD if comm is None else comm
        self.rank      = self.comm.rank
        self.size      = self.comm.size
                
        # need at least one
        if self.size == 1:
            raise ValueError("tried to create an MPI pool, but there "
                             "was only one MPI process available. "
                             "Need at least two.")

        # make the sub-communicator
        self._create_subcomm()
        self.status = MPI.Status()
        
        # communication tags
        self.tags = enum('READY', 'DONE', 'EXIT', 'START')

    def _create_subcomm(self):
        """
        Create the sub-communicator used by each independent worker when
        running the task function
        """
        self.subcomm = None
        chain_ranks = []
        color = 0
        total_ranks = 0
        
        # split the ranks
        for i, ranks in split_ranks(self.size, self.cpus_per_worker, include_all=self.use_all_cpus):
            chain_ranks.append(ranks[0])
            if self.rank in ranks: color = i+1
            total_ranks += len(ranks)
        
        self.workers = i+1 # store the total number of workers
        leftover = (self.size - 1) - total_ranks
        if leftover and self.rank == 0:
            args = (self.cpus_per_worker, self.size-1, leftover)
            self.logger.warning("with `cpus_per_worker` = %d and %d available ranks, %d ranks will do no work" %args)
            
        # crash if we only have one process or one worker
        if self.size <= self.workers:
            args = (self.size, self.workers+1, self.workers)
            raise ValueError("only have %d ranks; need at least %d to use the desired %d workers" %args)
            
        # ranks that will do work have a nonzero color now
        self._valid_worker = color > 0
        
        # split the comm between the workers
        self.subcomm = self.comm.Split(color, 0)
        
        # set the global extension point comm
        set_nbkit_comm(self.subcomm)
        
    def is_master(self):
        """
        Is the current process the master?
        """
        return self.rank == 0

    def is_worker(self):
        """
        Is the current process a valid worker (and thus should wait for
        instructions from the master)
        """
        return self._valid_worker
        
    def wait(self):
        """
        If this isn't the master process, wait for instructions.
        """
        if self.is_master():
            raise RuntimeError("master node told to await jobs")

        # must be valid worker to wait
        if not self.is_worker():
            self.logger.debug("rank %d process is done waiting" %self.rank)
            return

        # logging info
        if self.subcomm.rank == 0:
            args = (self.rank, MPI.Get_processor_name(), self.subcomm.size)
            self.logger.info("worker master rank is %d on %s with %d processes available" %args)

        # continously loop and wait for instructions
        while True:
            args = None
            tag = -1

            # have the master rank of the subcomm ask for task and then broadcast
            if self.subcomm.rank == 0:
                self.comm.send(None, dest=0, tag=self.tags.READY)
                args = self.comm.recv(source=0, tag=MPI.ANY_TAG, status=self.status)
                tag = self.status.Get_tag()
            
            # bcast to everyone in the worker subcomm
            args  = self.subcomm.bcast(args)
            tag   = self.subcomm.bcast(tag)

            # do the work here
            if tag == self.tags.START:
                result = self.function(*args)
                self.subcomm.Barrier() # wait for everyone in subcomm
                if self.subcomm.rank == 0:
                    self.comm.send([args[0], result], dest=0, tag=self.tags.DONE) # done this task
            elif tag == self.tags.EXIT:
                break

        # wait for everyone in subcomm and exit
        self.subcomm.Barrier()
        if self.subcomm.rank == 0:
            self.comm.send(None, dest=0, tag=self.tags.EXIT) # exiting
            
        # debug logging
        self.logger.debug("rank %d process is done waiting" %self.rank)

    def compute(self, tasks):
        """
        Compute a series of tasks. For each task, the function takes
        the iteration number, followed by the `task` value as the 
        arguments

        Returns
        -------
        results : list
            a list of the return values of the task function for 
            each task
        """
        ntasks = len(tasks)

        # everyone but master should wait for instructions
        if not self.is_master():
            self.wait()
            self.logger.debug("rank %d process finished" %self.rank)
            return

        # initialize
        task_index = 0
        closed_workers = 0
        results = []

        # logging info
        args = (self.workers, ntasks)
        self.logger.info("master starting with %d worker(s) with %d total tasks" %args)

        try:
    
            # loop until all workers have finished with no more tasks
            while closed_workers < self.workers:
                data = self.comm.recv(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG, status=self.status)
                source = self.status.Get_source()
                tag = self.status.Get_tag()
    
                # worker is ready, so send it a task
                if tag == self.tags.READY:
                    if task_index < ntasks:
                        this_task = [task_index, tasks[task_index]]
                        self.comm.send(this_task, dest=source, tag=self.tags.START)
                        self.logger.info("sending task `%s` to worker %d" %(str(tasks[task_index]), source))
                        task_index += 1
                    else:
                        self.comm.send(None, dest=source, tag=self.tags.EXIT)
                elif tag == self.tags.DONE:
                    results.append(data)
                    self.logger.debug("received result from worker %d" %source)
                elif tag == self.tags.EXIT:
                    closed_workers += 1
                    self.logger.debug("worker %d has exited, closed workers = %d" %(source, closed_workers))
                    
        except Exception as e:
            self.logger.error("an exception has occurred on one of the ranks...all ranks exiting")
            self.logger.error(traceback.format_exc())
            
            # close the pool 
            self.close()
            
            # bit of hack that forces mpi4py to exit all ranks
            # see https://groups.google.com/forum/embed/#!topic/mpi4py/RovYzJ8qkbc
            os._exit(1)  
            
        finally:
            # exit
            self.logger.debug("rank %d process finished" %self.rank)
            
        # return the results in sorted order
        return [r[1] for r in sorted(results, key=lambda r: r[0])]
        
    def close(self):
        """
        Close the pool by freeing the subcomm
        """
        if self.is_master():
            self.logger.info("master is finished; terminating")
            if self.subcomm is not None:
                self.subcomm.Free()
    
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()