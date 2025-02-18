import logging
from typing import Union, List, Iterable
import os
from abc import ABC, abstractmethod
from itertools import groupby
from operator import itemgetter
import re
from subprocess import CompletedProcess
import xml

import xmltodict

from pydock3.util import system_call, find_key_values_in_dict
from pydock3.files import File

#
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class JobScheduler(ABC):
    REQUIRED_ENV_VAR_NAMES = []

    def __init__(self, name):
        self.name = name

    @abstractmethod
    def submit(
            self,
            job_name: str,
            script_path: str,
            env_vars_dict: dict,
            log_dir_path: str,
            task_ids: Iterable[Union[str, int]],
            job_timeout_minutes: Union[int, None] = None,
            extra_submission_cmd_params_str: [str, None] = None
    ):
        """returns: subprocess.CompletedProcess"""

        raise NotImplementedError

    @abstractmethod
    def job_is_on_queue(self, job_name: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def task_is_on_queue(self, task_id: Union[str, int], job_name: str) -> bool:
        raise NotImplementedError


class SlurmJobScheduler(JobScheduler):
    REQUIRED_ENV_VAR_NAMES = [
        "SBATCH_EXEC",
        "SQUEUE_EXEC",
    ]

    def __init__(self) -> None:
        super().__init__(name="Slurm")

        # set required env vars
        self.SBATCH_EXEC = os.environ["SBATCH_EXEC"]
        self.SQUEUE_EXEC = os.environ["SQUEUE_EXEC"]

        # set optional env vars
        self.SLURM_SETTINGS = os.environ.get("SLURM_SETTINGS")

        #
        if self.SLURM_SETTINGS:
            system_call(f"source {self.SLURM_SETTINGS}")

    def submit(
            self,
            job_name: str,
            script_path: str,
            env_vars_dict: dict,
            log_dir_path: str,
            task_ids: Iterable[Union[str, int]],
            job_timeout_minutes: Union[int, None] = None,
            extra_submission_cmd_params_str: [str, None] = None,
    ) -> List[CompletedProcess]:
        #
        if extra_submission_cmd_params_str is None:
            extra_submission_cmd_params_str = ""

        #
        task_nums = sorted([int(task_id) for task_id in task_ids])
        contiguous_task_nums_sets = [list(map(itemgetter(1), g)) for k, g in groupby(enumerate(task_nums), lambda x: x[0] - x[1])]

        #
        procs = []
        max_chars_in_tasks_array_str = 10000
        curr_tasks_array_indices = []
        num_sets = len(contiguous_task_nums_sets)
        for i, contiguous_task_nums_set in enumerate(contiguous_task_nums_sets):
            if len(contiguous_task_nums_set) == 1:
                index_str = f"{contiguous_task_nums_set[0]}"
            else:
                index_str = f"{contiguous_task_nums_set[0]}-{contiguous_task_nums_set[-1]}"

            curr_tasks_array_indices_str = ",".join([str(x) for x in curr_tasks_array_indices + [index_str]])
            if(len(curr_tasks_array_indices_str) >= max_chars_in_tasks_array_str) or (i == num_sets - 1):
                command_str = f"{self.SBATCH_EXEC} --export=ALL -J {job_name} -o {log_dir_path}/{job_name}_%A_%a.out -e {log_dir_path}/{job_name}_%A_%a.err --signal=B:USR1@120 {extra_submission_cmd_params_str} --array={curr_tasks_array_indices_str}"  # TODO: is `signal` useful / necessary?
                curr_tasks_array_indices = []
            else:
                continue

            if job_timeout_minutes is not None:
                command_str += f" --time={job_timeout_minutes}"

            command_str += f" {script_path}"

            if self.SLURM_SETTINGS:
                if File.file_exists(self.SLURM_SETTINGS):  # TODO: move validation to __init__
                    command_str = f"source {self.SLURM_SETTINGS}; {command_str}"

            proc = system_call(
                command_str, env_vars_dict=env_vars_dict
            )  # need to pass env_vars_dict here so that '--export=ALL' in command can pass along all the env vars
            procs.append(proc)

        return procs

    def job_is_on_queue(self, job_name: str) -> bool:
        command_str = f"{self.SQUEUE_EXEC} --format='%i %j %t' | grep '{job_name}'"
        proc = system_call(command_str)

        #
        if proc.stdout:
            return True
        else:
            return False

    def task_is_on_queue(self, task_id: Union[str, int], job_name: str) -> bool:
        command_str = f"{self.SQUEUE_EXEC} -r --format='%i %j %t' | grep '{job_name}'"
        proc = system_call(command_str)

        #
        if not proc.stdout:
            return False

        #
        for line in proc.stdout.split('\n'):
            line_stripped = line.strip()
            if line_stripped:
                job_id, job_name, state = line_stripped.split()
                if job_id.endswith(f"_{task_id}"):
                    return True

        return False


class SGEJobScheduler(JobScheduler):
    REQUIRED_ENV_VAR_NAMES = [
        "QSUB_EXEC",
        "QSTAT_EXEC",
    ]

    def __init__(self) -> None:
        super().__init__(name="SGE")

        # set required env vars
        self.QSUB_EXEC = os.environ["QSUB_EXEC"]
        self.QSTAT_EXEC = os.environ["QSTAT_EXEC"]

        # set optional env vars
        self.SGE_SETTINGS = os.environ.get("SGE_SETTINGS")

        #
        if self.SGE_SETTINGS:
            system_call(f"source {self.SGE_SETTINGS}")

    def submit(
            self,
            job_name: str,
            script_path: str,
            env_vars_dict: dict,
            log_dir_path: str,
            task_ids: Iterable[Union[str, int]],
            job_timeout_minutes: Union[int, None] = None,
            extra_submission_cmd_params_str: [str, None] = None,
    ) -> List[CompletedProcess]:
        #
        if extra_submission_cmd_params_str is None:
            extra_submission_cmd_params_str = "-S /bin/bash -q !gpu.q"

        #
        if not job_name[0].isalpha():
            raise Exception(f"{self.name} job names must start with a letter.")

        #
        task_nums = sorted([int(task_id) for task_id in task_ids])
        contiguous_task_nums_sets = [list(map(itemgetter(1), g)) for k, g in groupby(enumerate(task_nums), lambda x: x[0] - x[1])]

        #
        procs = []
        for contiguous_task_nums_set in contiguous_task_nums_sets:
            if len(contiguous_task_nums_set) == 1:
                array_str = f"{contiguous_task_nums_set[0]}"
            else:
                array_str = f"{contiguous_task_nums_set[0]}-{contiguous_task_nums_set[-1]}"

            command_str = f"{self.QSUB_EXEC} -V -N {job_name} -o {log_dir_path} -e {log_dir_path} -cwd {extra_submission_cmd_params_str} -t {array_str}"

            if job_timeout_minutes is not None:
                job_timeout_seconds = 60 * job_timeout_minutes
                command_str += (
                    f" -l s_rt={job_timeout_seconds} -l h_rt={job_timeout_seconds}"
                )

            command_str += f" {script_path}"

            if self.SGE_SETTINGS:
                if File.file_exists(self.SGE_SETTINGS):  # TODO: move validation to __init__
                    command_str = f"source {self.SGE_SETTINGS}; {command_str}"

            proc = system_call(
                command_str, env_vars_dict=env_vars_dict
            )  # need to pass env_vars_dict here so that '-V' in command can pass along all the env vars
            procs.append(proc)

        return procs

    def job_is_on_queue(self, job_name: str) -> bool:
        command_str = f"{self.QSTAT_EXEC} -r | grep '{job_name}'"
        proc = system_call(command_str)
        if proc.stdout:
            return True
        else:
            return False

    def _get_qstat_xml_as_dict(self) -> dict:
        command_str = f"{self.QSTAT_EXEC} -xml"
        proc = system_call(command_str)
        if proc.stdout is None:
            raise Exception(f"Command '{command_str}' returned stdout of None. stderr: {proc.stderr}")
        try:
            return xmltodict.parse(proc.stdout)
        except xml.parsers.expat.ExpatError as e:
            raise Exception(f"Error parsing XML from command '{command_str}'. \nstdout: \n{proc.stdout}\n\nstderr: {proc.stderr}") from e

    def task_is_on_queue(self, task_id: Union[str, int], job_name: str) -> bool:
        task_num = int(task_id)

        #
        q_dict = self._get_qstat_xml_as_dict()

        #
        job_list_values = find_key_values_in_dict(q_dict, key='job_list')

        #
        job_dicts = []
        for obj in job_list_values:
            if isinstance(obj, dict):
                job_dicts += [obj]
            elif isinstance(obj, list):
                job_dicts += obj
            else:
                raise Exception(f"Unexpected type for `job_list`: {type(obj)}")

        #
        for job_dict in job_dicts:
            #
            if not job_dict.get('JB_name') == job_name:
                continue

            #
            tasks_str = job_dict.get('tasks')
            if tasks_str is None:
                continue

            #
            pattern = r'^(\d+)[,-](\d+)(:\d+)?$'
            match = re.match(pattern, tasks_str)
            if match is not None:
                if match.group(1) is not None and match.group(2) is not None:
                    start = int(match.group(1))
                    end = int(match.group(2))
                    if (task_num >= start) and (task_num <= end):
                        #
                        return True

            #
            pattern = r'^(\d+)$'
            match = re.match(pattern, tasks_str)
            if match is not None:
                if match.group(1) is not None:
                    num = int(match.group(1))
                    if task_num == num:
                        return True

        #
        return False
