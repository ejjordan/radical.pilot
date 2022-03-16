
__copyright__ = 'Copyright 2022, The RADICAL-Cybertools Team'
__license__   = 'MIT'

import inspect
import functools

from .utils import serialize_obj, serialize_bson


class PythonTask(object):

    def __new__(cls, func, *args, **kwargs):
        """
        We handle wrapped functions here with no args or kwargs.
        Example:
        import PythonTask
        wrapped_func   = partial(func_A, func_AB)      
        cud.EXECUTABLE = PythonTask(wrapped_func)
        """
        if not inspect.isfunction(func):
            raise ValueError('task function not callable')

        ser_func = serialize_obj(func)
        TASK = {'func'  :ser_func,
                'args'  :args,
                'kwargs':kwargs}
        try:
            SER_TASK = serialize_bson(TASK)
            return SER_TASK
        except Exception as e:
            raise ValueError(e)

    def pythontask(f):
        """
        We handle all other functions here.
        Example:
        from PythonTask import pythonfunc as pythonfunc
        @pythontask
        def func_C(x):
            return (x)
        cud.EXECUTABLE = func_C(2)
        """

        if not inspect.isfunction(f):
            raise ValueError('task function not callable')

        @functools.wraps(f)
        def decor(*args, **kwargs): 
            ser_func = serialize_obj(f)

            TASK = {'func'  :ser_func,
                    'args'  :args,
                    'kwargs':kwargs}
            try:
                SER_TASK = serialize_bson(TASK)
                return SER_TASK
            except Exception as e:
                raise ValueError ('failed to wrap function') from e
        return decor
