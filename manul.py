#   manul - main module
#   -------------------------------------
#   Written and maintained by Maksim Shudrak <mshudrak@salesforce.com> <mxmssh@gmail.com>
#
#   Copyright 2019 Salesforce.com, inc. All rights reserved.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at:
#     http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

from os import listdir
from os.path import isfile, join
import shutil
from ctypes import *
import multiprocessing
import argparse
from timeit import default_timer as timer
import ntpath
from printing import *
from manul_utils import *
from manul_win_utils import *
import manul_network
import random
import afl_fuzz
import zlib
import importlib

PY3 = sys.version_info[0] == 3

if PY3:
    string_types = str,
    xrange = range
else:
    string_types = basestring,
    xrange = xrange

import subprocess, threading
import signal

#TODO: communication with process + error codes would be better to implement using AFL's instrumentation code
# and shared memory

original_process = None


class Command(object):
    def __init__(self, cmd, target_ip, target_port, target_protocol):
        self.cmd = cmd
        self.process = None
        self.out = None
        self.err = None
        self.target_ip = target_ip
        self.target_port = target_port
        self.target_protocol = target_protocol #TODO: it would be better to init this Command class once at dry run and run it over and over

    def exec_command(self):
        global original_process
        if sys.platform == "win32":
            self.process = subprocess.Popen(self.cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        else:
            self.process = subprocess.Popen(self.cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            preexec_fn=os.setsid)
        if self.target_ip:
            original_process = True
        INFO(1, None, None, "Target started, waiting for return")

        self.out, self.err = self.process.communicate() # TODO: in python 3 we can put timeout here and avoid watchdog

        if self.target_ip:
            original_process = (self.out, self.err, self.process.returncode)

    def run(self):
        global original_process

        if self.target_ip:
            if not original_process: #is it a first run ?
                self.exec_command()
                time.sleep(0.5) #TODO: introduce user-defined sleep
                self.cmd= self.cmd.split(" ")[1] # TODO: fix it!!!!!!!!!
                return self.run()

            else: # in this case, cmd contains data
                manul_network.send_test_case(self.cmd, self.target_ip, self.target_port, self.target_protocol)
                # TODO: we should establish some wait here until the target finish parsing our test case
                if isinstance(original_process, tuple):
                    out, err, returncode = original_process
                    return returncode, err
                return 0, ""

        self.exec_command()

        if isinstance(self.err, (bytes, bytearray)):
            self.err = self.err.decode("utf-8", 'replace')
        return self.process.returncode, self.err


class Fuzzer:
    def __init__(self, list_of_files, fuzzer_id, virgin_bits_global, args, stats_array, restore_session, crash_bits,
                 dbi_setup):
        # local fuzzer config
        global SHM_SIZE
        self.SHM_SIZE = SHM_SIZE
        self.CALIBRATIONS_COUNT = 7
        self.SHM_ENV_VAR = "__AFL_SHM_ID"
        self.dbi = args.dbi
        self.afl_fuzzer = dict()
        self.token_dict = list()


        self.user_mutators = dict()
        self.mutator_weights = OrderedDict()
        total_weights = 0
        try:
            weights = args.mutator_weights.split(",")
            for weight in weights:
                name, weight = weight.split(":")
                total_weights += int(weight)
                self.mutator_weights[name] = total_weights
        except:
            ERROR("Invalid format for mutator_weights string, check manul.config file")

        if total_weights != 10:
            ERROR("Weights in mutator_weights should have 10 in sum, check manul.config file")

        try:
            if args.dict:
                fd = open(args.dict, 'r')
                content = fd.readlines()
                fd.close()
                for line in content:
                    line = line.replace("\n", "")
                    if line.startswith("#") or line == "":
                        continue
                    line = bytearray(line, "utf-8")
                    self.token_dict.append(line)
        except:
            WARNING(None, "Failed to parse dictionary file, dictionary is in invalid format or not accessible")

        self.current_file_name = None
        self.prev_hashes = dict() # used to store hash of coverage bitmap for each fil
        for file_name in list_of_files:
            self.prev_hashes[file_name] = None

        self.cmd_fuzzing = args.cmd_fuzzing

        if args.user_signals:
            self.user_defined_signals = args.user_signals.split(",")
        else:
            self.user_defined_signals = None

        if dbi_setup:
            self.dbi_engine_path = dbi_setup[0]
            self.dbi_tool_path = dbi_setup[1]
            self.dbi_tool_params = dbi_setup[2]

        self.target_ip = None
        self.target_port = None
        self.target_protocol = None
        if args.target_ip_port:
            self.target_ip = args.target_ip_port.split(':')[0] # todo check if ip:port is correct in main
            self.target_port = args.target_ip_port.split(':')[1]
            self.target_protocol = args.target_protocol

        self.list_of_files = list_of_files
        self.fuzzer_id = fuzzer_id
        self.virgin_bits = list()
        self.virgin_bits = [0xFF] * SHM_SIZE

        self.global_map = virgin_bits_global
        self.crash_bits = crash_bits  # happens not too often

        self.timeout = args.timeout
        self.stats_array = stats_array
        self.restore = restore_session

        self.determenistic = args.determinstic_seed
        if self.determenistic:
            random.seed(a=self.fuzzer_id)

        # creating output dir structure
        self.output_path = args.output + "/%d" % fuzzer_id
        self.queue_path = self.output_path + "/queue"
        self.crashes_path = self.output_path + "/crashes"
        self.unique_crashes_path = self.crashes_path + "/unique"

        if not self.restore:
            try:
                os.mkdir(self.output_path)
            except:
                ERROR("Failed to create required output dir structure (unique dir)")
            try:
                os.mkdir(self.queue_path)
            except:
                ERROR("Failed to create required output dir structure (queue)")
            try:
                os.mkdir(self.crashes_path)
            except:
                ERROR("Failed to create required output dir structure (crashes)")
            try:
                os.mkdir(self.unique_crashes_path)
            except:
                ERROR("Failed to create required output dir structure (unique crashes)")

        self.is_dumb_mode = args.simple_mode
        self.input_path = args.input
        self.target_binary_path = args.target_binary # and its arguments

        self.fuzzer_stats = FuzzerStats()
        self.stats_file = None
        if self.restore:
            if not isfile(self.output_path + "/fuzzer_stats"):
                ERROR("Fuzzer stats file doesn't exist. Make sure your output is actual working dir of manul")

            self.stats_file = open(self.output_path + "/fuzzer_stats", 'r')
            content = self.stats_file.readlines()
            line = None
            for line in content: # getting last line from file to restore ression
                pass
            if line is None:
                ERROR("Failed to restore fuzzer %d from stats. Invalid fuzzeR_stats format" % self.fuzzer_id)

            last = line[:-2] # skipping last symbol space and \n
            INFO(0, None, None, "Restoring last stats %s" % last)
            self.stats_file.close()
            self.restore_session(last)

            self.stats_file = open(self.output_path + "/fuzzer_stats", 'a')
        else:
            self.stats_file = open(self.output_path + "/fuzzer_stats", 'w')

        self.enable_logging = args.logging_enable
        self.log_file = None

        self.user_sync_freq = args.sync_freq
        self.sync_bitmap_freq = -1

        if self.enable_logging:
            self.log_file = open(self.output_path + "/fuzzer_log", 'a')

        if not self.is_dumb_mode:
            self.trace_bits = self.setup_shm()

            for i in range(0, self.SHM_SIZE):
                if self.virgin_bits[i] != 0xFF:
                    self.global_map[i] = self.virgin_bits[i]
                elif self.global_map[i] != 0xFF and self.virgin_bits[i] == 0xFF:
                    self.virgin_bits[i] = self.global_map[i]
        self.init_mutators()

    def sync_bitmap(self):
        self.sync_bitmap_freq += 1
        if (self.sync_bitmap_freq % self.user_sync_freq) != 0:
            return
        if self.is_dumb_mode:
            return
        for i in range(0, self.SHM_SIZE):
            if self.virgin_bits[i] != 0xFF:
                self.global_map[i] = self.virgin_bits[i]
            elif self.global_map[i] != 0xFF and self.virgin_bits[i] == 0xFF:
                self.virgin_bits[i] = self.global_map[i]


    def restore_session(self, last):
        # parse previously saved stats line
        last = last.split(" ")[1:] # cut timestamp
        for index, stat in enumerate(last):
            stat = float(stat.split(":")[1])  # taking actual value
            # TODO: i#10 This is not compatible with Python 3.
            stat_name = self.fuzzer_stats.stats.items()[index][0]
            self.fuzzer_stats.stats[stat_name] = stat

        if self.determenistic: # skip already seen seeds
            for i in range(0, self.fuzzer_stats.stats['executions']):
                random.seed(seed=self.fuzzer_id)


    def save_stats(self):
        if self.stats_file is None:
            return

        self.stats_file.write(str(time.time()) + " ")
        for index, (k,v) in enumerate(self.fuzzer_stats.stats.items()):
            self.stats_file.write("%d:%.2f " % (index, v))
        self.stats_file.write("\n")


    def prepare_cmd_to_run(self, target_file_path):
        if self.dbi:
            dbi_tool_opt = "-c"
            if self.dbi == "pin":
                dbi_tool_opt = "-t"

            binary_path = "".join(self.target_binary_path)
            if self.cmd_fuzzing:
                target_file_path = extract_content(target_file_path) # now it is the file content

            binary_path = binary_path.replace("@@", target_file_path)

            final_string = "%s %s %s %s -- %s" % (self.dbi_engine_path, dbi_tool_opt, self.dbi_tool_path,
                                                  self.dbi_tool_params, binary_path)
        else:
            final_string = "".join(self.target_binary_path)
            if self.cmd_fuzzing:
                target_file_path = extract_content(target_file_path) # now it is the file content

            final_string = final_string.replace("@@", target_file_path)

        return final_string


    def setup_shm_win(self):
        FILE_MAP_ALL_ACCESS = 0xF001F
        INVALID_HANDLE_VALUE = 0xFFFFFFFF
        PAGE_READWRITE = 0x04
        sh_name = "%s_%s" % (str(int(round(time.time()))), self.fuzzer_id)
        szName = c_char_p(sh_name.encode("utf-8"))

        hMapObject = windll.kernel32.CreateFileMappingA(INVALID_HANDLE_VALUE, None,
                                                        PAGE_READWRITE, 0, self.SHM_SIZE,
                                                        szName)
        if hMapObject == 0:
            ERROR("Could not open file mapping object")

        pBuf = windll.kernel32.MapViewOfFile(hMapObject, FILE_MAP_ALL_ACCESS, 0, 0,
                                             self.SHM_SIZE)
        if pBuf == 0:
            ERROR("Could not map view of file")

        INFO(0, None, self.log_file, "Setting up shared mem %s for fuzzer:%d" % (sh_name,
             self.fuzzer_id))
        os.environ[self.SHM_ENV_VAR] = sh_name

        return pBuf


    def setup_shm(self):
        if sys.platform == "win32":
            return self.setup_shm_win()

        IPC_PRIVATE = 0

        try:
            rt = CDLL('librt.so')
        except:
            rt = CDLL('librt.so.1')

        shmget = rt.shmget
        shmget.argtypes = [c_int, c_size_t, c_int]
        shmget.restype = c_int
        shmat = rt.shmat
        shmat.argtypes = [c_int, POINTER(c_void_p), c_int]
        shmat.restype = c_void_p#POINTER(c_byte * self.SHM_SIZE)

        shmid = shmget(IPC_PRIVATE, self.SHM_SIZE, 0o666)
        if shmid < 0:
            ERROR("shmget() failed")

        addr = shmat(shmid, None, 0)

        INFO(0, None, self.log_file, "Setting up shared mem %d for fuzzer:%d" % (shmid, self.fuzzer_id))
        os.environ[self.SHM_ENV_VAR] = str(shmid)

        return addr


    def init_mutators(self):
        INFO(0, bcolors.BOLD + bcolors.HEADER, self.log_file, "Initializing mutators")

        for module_name in self.mutator_weights:
            if "afl" == module_name or "radamsa" == module_name:
                continue
            try:
                self.user_mutators[module_name] = importlib.import_module(module_name)
            except ImportError as exc:
                ERROR("Unable to load user provided mutator %s. %s" % (module_name, exc.message))

            self.user_mutators[module_name].init()

        #init AFL fuzzer state
        for file_name in self.list_of_files:
            self.afl_fuzzer[file_name] = afl_fuzz.AFLFuzzer(self.token_dict, self.queue_path) #assign AFL for each file


    def dry_run(self):

        INFO(0, bcolors.BOLD + bcolors.HEADER, self.log_file, "Performing dry run")

        useless = 0
        for file_name in self.list_of_files:
            self.current_file_name = file_name
            memset(self.trace_bits, 0x0, SHM_SIZE)

            cmd = self.prepare_cmd_to_run(self.input_path + "/" + file_name)

            INFO(1, bcolors.BOLD, self.log_file, "Launching %s" % cmd)

            command = Command(cmd, self.target_ip, self.target_port, self.target_protocol)
            err_code, err_output = command.run()
            if err_code != 0:
                INFO(1, None, self.log_file, "Initial input %s triggers exception in the target" % file_name)
                if self.is_critical(err_output, err_code):
                    WARNING(self.log_file, "Initial input %s leads target to crash (did you disable leak sanitizer?). Enable -d or -l to check actual output" % file_name)
                    INFO(1, None, self.log_file, err_output)
                elif self.is_problem_with_config(err_code, err_output):
                    WARNING(self.log_file, "Problematic file %s" % file_name)

            trace_bits_as_str = string_at(self.trace_bits, SHM_SIZE)

            # count non-zero bytes just to check that instrumentation actually works
            non_zeros = [x for x in trace_bits_as_str if x != 0x0]
            if len(non_zeros) == 0:
                INFO(1, None, self.log_file, "Output from target %s" % err_output)
                ERROR("%s doesn't cover any path in the target, Make sure the binary is actually instrumented" % file_name)

            ret = self.has_new_bits(trace_bits_as_str, True, list(), self.virgin_bits, False)
            if ret == 0:
                useless += 1
                WARNING(self.log_file, "Test %s might be useless because it doesn't cover new paths in the target, consider removing it" % file_name)
            else:
                self.sync_bitmap()

        if useless != 0:
            WARNING(self.log_file, "%d out of %d initial files are useless" % (useless, len(self.list_of_files)))

        INFO(0, bcolors.BOLD + bcolors.OKBLUE, self.log_file, "Dry run finished")
        self.fuzzer_stats.stats['executions'] += 1.0
        self.update_stats()


    def has_new_bits(self, trace_bits_as_str, update_virgin_bits, volatile_bytes, bitmap_to_compare, calibration):

        ret = 0

        if not calibration:
            hash_current = zlib.crc32(trace_bits_as_str) & 0xFFFFFFFF

            if not isinstance(self.current_file_name, string_types):
                self.current_file_name = self.current_file_name[1]

            prev_hash = self.prev_hashes[self.current_file_name]

            if prev_hash and hash_current == prev_hash:
                return 0
            self.prev_hashes[self.current_file_name] = hash_current

        for j in range(0, SHM_SIZE):
            if j in volatile_bytes:
                continue  # ignoring volatile bytes

            if PY3:
                trace_byte = trace_bits_as_str[j] # optimize it and compare by 4-8 bytes or even use xmm0?
            else:
                trace_byte = ord(trace_bits_as_str[j]) # self.trace_bits.contents[j])#

            if not trace_byte:
                continue

            virgin_byte = bitmap_to_compare[j]

            if trace_byte and (trace_byte & virgin_byte):
                if ret < 2:
                    if virgin_byte == 0xff:
                        ret = 2  # new path discovered
                    else:
                        ret = 1  # new hit of existent paths

                virgin_byte = virgin_byte & ~trace_byte

                if update_virgin_bits:
                    bitmap_to_compare[j] = virgin_byte # python will handle potential syncronization issues

        return ret

    def calibrate_test_case(self, file_name):
        volatile_bytes = list()
        trace_bits_as_str = string_at(self.trace_bits, self.SHM_SIZE) # this is how we read memory in Python

        bitmap_to_compare = list("\x00" * self.SHM_SIZE)
        for i in range(0, self.SHM_SIZE):
            if PY3:
                bitmap_to_compare[i] = trace_bits_as_str[i]
            else:
                bitmap_to_compare[i] = ord(trace_bits_as_str[i])

        for i in range(0, self.CALIBRATIONS_COUNT):
            INFO(1, None, self.log_file, "Calibrating %s %d" % (file_name, i))

            memset(self.trace_bits, 0x0, SHM_SIZE)
            if self.target_ip: # in net mode we only need a test case
                cmd = self.prepare_cmd_to_run(file_name)
            else:
                cmd = self.prepare_cmd_to_run(self.queue_path + "/" + file_name)

            INFO(1, None, self.log_file, cmd)
            command = Command(cmd, self.target_ip, self.target_port, self.target_protocol)
            if self.cmd_fuzzing:
                try:
                    err_code, err_output = command.run()
                except TypeError:
                    WARNING("Failed to send this input over command line into the target")
                    continue
            else:
                err_code, err_output = command.run()

            if err_code > 0:
                INFO(1, None, self.log_file, "Target raised exception during calibration for %s" % file_name)

            trace_bits_as_str = string_at(self.trace_bits, SHM_SIZE) # this is how we read memory in Python

            for j in range(0, SHM_SIZE):
                if PY3:
                    trace_byte = trace_bits_as_str[j]
                else:
                    trace_byte = ord(trace_bits_as_str[j])
                if trace_byte != bitmap_to_compare[j]:
                    if j not in volatile_bytes:
                        volatile_bytes.append(j) # mark offset of this byte as volatile

        INFO(1, None, self.log_file, "We have %d volatile bytes for this new finding" % len(volatile_bytes))
        # let's try to check for new coverage ignoring volatile bytes
        self.fuzzer_stats.stats['blacklisted_paths'] = len(volatile_bytes)
        return self.has_new_bits(trace_bits_as_str, True, volatile_bytes, self.virgin_bits, True) # it means that we're returning result of the last run (might be wrong)

    def update_stats(self):
        for i, (k,v) in enumerate(self.fuzzer_stats.stats.items()):
            self.stats_array[i] = v

    def is_problem_with_config(self, exc_code, err_output):
        if exc_code == 127 or exc_code == 126: # command not found or permissions
            ERROR("Thread %d unable to execute target. Bash return %s" % (self.fuzzer_id, err_output))
        elif exc_code == 124: #timeout
            WARNING(self.log_file, "Target failed to finish execution within given timeout, try to increase default timeout")
            return True
        return False

    def generate_new_name(self, file_name):
        iteration = int(round(self.fuzzer_stats.stats['executions']))
        if file_name.startswith("manul"): # manul-DateTime-FuzzerId-iteration_original.name
            base_name = file_name[file_name.find("_")+1:]
            file_name = base_name

        now = int(round(time.time()))
        return "manul-%d-%d-%d_%s" % (now, self.fuzzer_id, iteration, file_name)

    def is_critical_win(self, exception_code):

        if exception_code == STATUS_CONTROL_C_EXIT:
            return False

        if exception_code >= EXCEPTION_FIRST_CRITICAL_CODE and exception_code < EXCEPTION_LAST_CRITICAL_CODE:
            return True

        return False

    def is_critical_mac(self, exception_code): # todo; we need to verify that on MacOS
        if exception_code == 0 or exception_code == 1 or \
           (IGNORE_ABORT and exception_code == signal.SIGABRT) or \
           exception_code == signal.SIGKILL or \
           exception_code == signal.SIGUSR1 or \
           exception_code == signal.SIGUSR2 or \
           exception_code == signal.SIGALRM or \
           exception_code == signal.SIGCHLD:
            return False
        return True

    def is_critifcal_linux(self, err_code):
        if err_code in critical_signals_linux:
            return True
        if self.user_defined_signals and err_code in self.user_defined_signals:
            return True

        return False

    def is_critical(self, err_str, err_code):
        if "Sanitizer" in err_str or "SIGSEGV" in err_str or "Segmentation fault" in err_str or \
           "core dumped" in err_str or "floating point exception" in err_str:
            return True

        # TODO: use user specified signals provided via config option: user_signal to ignore certain signals
        if sys.platform == "win32":
            return self.is_critical_win(err_code)
        elif sys.platform == "darwin":
            return self.is_critical_mac(err_code)
        else: # looks like Linux
            return self.is_critifcal_linux(err_code)

    def mutate_radamsa(self, full_input_file_path, full_output_file_path):
        new_seed_str = ""
        if self.determenistic:
            new_seed = random.randint(0, sys.maxsize)
            new_seed_str = "--seed %d " % new_seed

        cmd = "radamsa %s%s > %s" % (new_seed_str, full_input_file_path, full_output_file_path)

        INFO(1, None, self.log_file, "Running %s" % cmd)
        try:
            subprocess.check_output(cmd, stderr=subprocess.STDOUT, shell=True)  # generate new input
        except subprocess.CalledProcessError as exc:
            WARNING(self.log_file,
                    "Fuzzer %d failed to generate new input from %s due to some problem with radamsa. Error code %d. Return msg %s" %
                    (self.fuzzer_id, full_input_file_path, exc.returncode, exc.output))
            return 1
        return 0

    def mutate_afl(self, file_name, full_input_file_path, full_output_file_path):
        data = extract_content(full_input_file_path)
        res = self.afl_fuzzer[file_name].mutate(data, self.list_of_files)
        if not res:
            WARNING(self.log_file, "Unable to mutate data provided using afl")
            return 1
        if len(data) <= 0:
            WARNING(self.log_file, "AFL produced empty file for %s", full_input_file_path)

        save_content(data, full_output_file_path)
        return 0

    def mutate_input(self, file_name, full_input_file_path, full_output_file_path):
        execution = self.fuzzer_stats.stats['executions'] % 10
        for name in self.mutator_weights:
            weight = self.mutator_weights[name]
            if execution < weight and name == "afl":
                return self.mutate_afl(file_name, full_input_file_path, full_output_file_path)
            elif execution < weight and name == "radamsa":
                return self.mutate_radamsa(full_input_file_path, full_output_file_path)
            elif execution < weight:
                mutator = self.user_mutators.get(name, None)
                if not mutator:
                    ERROR("Unable to load user provided mutator %s at mutate_input stage" % name)
                data = extract_content(full_input_file_path)
                data = mutator.mutate(data) # todo handle potential errors here
                if not data:
                    ERROR("No data returned from user provided mutator. Exciting.")
                save_content(data, full_output_file_path)
                return 0
            else:
                continue

    def run(self):
        if not self.is_dumb_mode:
            self.dry_run()

        last_stats_saved_time = 0
        first_iteration = True

        if self.restore:
            INFO(0, bcolors.BOLD + bcolors.OKBLUE, self.log_file, "Session sucessfully restored")

        start_time = timer()

        while True: # never return
            new_files = list() # empty the list
            elapsed = 0

            for i, file_name in enumerate(self.list_of_files):
                self.current_file_name = file_name
                crash_found = False
                self.fuzzer_stats.stats['file_running'] = i

                full_input_file_path = self.input_path + "/"
                # if we have tuple and not string here it means that this file was found during execution and located in queue
                if not isinstance(file_name, string_types):
                    file_name = file_name[1]
                    full_input_file_path = self.queue_path + "/"
                full_input_file_path += file_name

                if not self.is_dumb_mode:
                    memset(self.trace_bits, 0x0, SHM_SIZE) # preparing our bitmap for new run

                mutated_name = file_name + "_mutated"
                full_output_file_path = self.queue_path + "/" + mutated_name

                # command to generate new input using one of selected mutators
                res = self.mutate_input(file_name, full_input_file_path, full_output_file_path)

                if res != 0:
                    ERROR("Fuzzer %d failed to generate and save new input on disk" % self.fuzzer_id)

                if self.target_ip and first_iteration == False:
                    cmd = full_output_file_path
                    INFO(1, None, self.log_file, "Sending over network content of %s" % cmd)
                else:
                    cmd = self.prepare_cmd_to_run(full_output_file_path)
                    first_iteration = False

                    INFO(1, None, self.log_file, "Running %s" % cmd)
                timer_start = timer()

                command = Command(cmd, self.target_ip, self.target_port, self.target_protocol)

                if self.cmd_fuzzing:
                    try:
                        exc_code, err_output = command.run()
                    except TypeError:
                        WARNING("Failed to give this input from bash into the target")
                        continue
                else:
                    exc_code, err_output = command.run()

                self.fuzzer_stats.stats['executions'] += 1.0
                elapsed += (timer() - timer_start)

                if exc_code != 0:
                    self.fuzzer_stats.stats['exceptions'] += 1
                    INFO(1, None, self.log_file, "Target raised exception and returns 0x%x error code" % (exc_code))

                    if self.is_critical(err_output, exc_code):
                        INFO(0, bcolors.BOLD + bcolors.OKGREEN, self.log_file, "New crash found by fuzzer %d" % self.fuzzer_id)
                        self.fuzzer_stats.stats["last_crash_time"] = time.time()

                        new_name = self.generate_new_name(file_name)
                        shutil.copy(full_output_file_path, self.crashes_path + "/" + new_name) # copying into crash folder
                        self.fuzzer_stats.stats['crashes'] += 1

                        if not self.is_dumb_mode:
                            trace_bits_as_str = string_at(self.trace_bits, SHM_SIZE) # this is how we read memory in Python
                            ret = self.has_new_bits(trace_bits_as_str, True, list(), self.crash_bits, False)
                            if ret == 2:
                                INFO(0, bcolors.BOLD + bcolors.OKGREEN, self.log_file, "Crash is unique")
                                self.fuzzer_stats.stats['unique_crashes'] += 1
                                shutil.copy(full_output_file_path, self.unique_crashes_path + "/" + new_name) # copying into crash folder with unique crashes

                        crash_found = True

                    elif self.is_problem_with_config(exc_code, err_output):
                        WARNING(self.log_file, "Problematic file: %s" % file_name)

                if not crash_found and not self.is_dumb_mode:
                    # Reading the coverage

                    trace_bits_as_str = string_at(self.trace_bits, SHM_SIZE) # this is how we read memory in Python
                    ret = self.has_new_bits(trace_bits_as_str, False, list(), self.virgin_bits, False) # we are not ready to update coverage at this stage due to volatile bytes
                    if ret == 2:
                        INFO(1, None, self.log_file, "Input %s produces new coverage, calibrating" % file_name)
                        if self.calibrate_test_case(mutated_name) == 2:
                            self.fuzzer_stats.stats['new_paths'] += 1
                            self.fuzzer_stats.stats['last_path_time'] = time.time()
                            INFO(1, None, self.log_file, "Calibration finished sucessfully. Saving new finding")
                            new_coverage_file_name = self.generate_new_name(file_name)
                            INFO(1, None, self.log_file, "Copying %s to %s" % (full_output_file_path,
                                 self.queue_path + "/" + new_coverage_file_name))
                            shutil.copy(full_output_file_path, self.queue_path + "/" + new_coverage_file_name)
                            new_files.append((1, new_coverage_file_name))
                            # for each new file assign new AFLFuzzer
                            self.afl_fuzzer[new_coverage_file_name] = afl_fuzz.AFLFuzzer(self.token_dict, self.queue_path)
                            self.prev_hashes[new_coverage_file_name] = None

                self.update_stats()

            self.sync_bitmap()

            if len(new_files) > 0:
                self.list_of_files = self.list_of_files + new_files

            self.fuzzer_stats.stats['files_in_queue'] = len(self.list_of_files)

            self.update_stats()

            end_time = timer() - start_time
            self.fuzzer_stats.stats['exec_per_sec'] = self.fuzzer_stats.stats['executions'] / end_time

            last_stats_saved_time += elapsed
            if last_stats_saved_time > 1: # we save fuzzer stats per iteration or once per second to avoid huge stats files
                self.save_stats()
                last_stats_saved_time = 0


def get_bytes_covered(virgin_bits):
    non_zeros = [x for x in virgin_bits if x != 0xFF]
    return len(non_zeros)


def run_fuzzer_instance(files_list, i, virgin_bits, args, stats_array, restore_session, crash_bits, dbi_setup):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    printing.DEBUG_PRINT = args.debug # FYI, multiprocessing causes global vars to be reinitialized.
    INFO(0, None, None, "Starting fuzzer %d" % i)
    if args.dbi:
        args.timeout *= 30 #DBI introduce ~30x overhead

    fuzzer_instance = Fuzzer(files_list, i, virgin_bits, args, stats_array, restore_session, crash_bits, dbi_setup)
    fuzzer_instance.run() # never return


def check_instrumentation(target_binary):
    with open(target_binary, 'rb') as f:
        s = f.read()
        res = s.find(b"__AFL_SHM_ID")
        if res == -1:
            return False
    return True


def which(target_binary):
    def is_binary(target_binary):
        return os.path.isfile(target_binary) and os.access(target_binary, os.X_OK)

    fpath, fname = os.path.split(target_binary)
    if fpath:
        if is_binary(target_binary):
            return target_binary
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            exec_file = os.path.join(path, target_binary)
            if is_binary(exec_file):
                return exec_file

    return None


def check_binary(target_binary):
    binary_path = which(target_binary)
    if binary_path is None:
        ERROR("Unable to find binary %s required to run Manul" % target_binary)


def get_available_id_for_backup(dir_name):
    id = 0
    tmp = dir_name + "_%d" % id
    while True:
        if not os.path.exists(tmp):
            return id
        id += 1
        tmp = dir_name + "_%d" % id


def configure_dbi(args, target_binary, is_debug):
    dbi_engine_path = args.dbi_root
    dbi_tool_path = args.dbi_client_root
    dbi_tool_libs = args.dbi_client_libs

    if dbi_engine_path is None or dbi_tool_path is None:
        ERROR("DBI_ROOT and/or DBI_CLIENT_ROOT paths not specified, unable to execute manul")

    check_binary(dbi_engine_path)
    check_binary(dbi_tool_path)

    dbi_tool_params = ""
    if args.dbi == "dynamorio":
        #TODO i#12: corner case is not handled here
        dbi_tool_params += "-coverage_module %s " % ntpath.basename(target_binary)
        if dbi_tool_libs is not None:
            for target_lib in dbi_tool_libs.split(","):
                if target_lib == "":
                    continue
                dbi_tool_params += "-coverage_module %s " % target_lib
        if is_debug:
            dbi_tool_params += "-debug"
    elif args.dbi == "pin":
        if sys.platform == "win32":
            ERROR("Intel PIN DBI engine is not supported on Windows")
        if dbi_tool_libs is not None:
            # adding desired libs to instrument
            fd = open("dbi_config", 'w')
            fd.write(dbi_tool_libs)
            fd.close()
            dbi_config_file_path = os.path.abspath("dbi_config")
            dbi_tool_params += " -libs %s" % dbi_config_file_path
    else:
        ERROR("Unknown dbi engine/option specified. Intel PIN or DynamoRIO are only supported")
    dbi_setup = (dbi_engine_path, dbi_tool_path, dbi_tool_params)
    return dbi_setup

def split_files_by_count(files_list, threads_count):
    # split list of input files by fuzzer instances
    less_files = False
    if len(files_list) < threads_count:
        less_files = True
        WARNING(None, "Too many fuzzing instances for %d files, same files will be mutated with different seeds" % len(files_list))

    files = [[] for x in xrange(threads_count)]
    thread_index = 0
    # if list of files is less than number of required threads we run our fuzzer with different seed on the same files
    while thread_index < threads_count:
        for i, file_name in enumerate(files_list):
            if less_files and (thread_index + i) >= threads_count:
                break
            piece = (thread_index + i) % threads_count
            files[piece].append(file_name)
            thread_index += i + 1
    return files


def get_files_list(path):
    files_list = [f for f in listdir(path) if isfile(join(path, f))]  # let's process input directory
    files_list.sort()

    if len(files_list) == 0:
        ERROR("No files for fuzzing, exiting")

    return files_list


def check_if_exist(files_list, path):
    for file_name in files_list:
        if file_name == "":
            ERROR("File list has empty file name")
        elif isfile(path + "/" + file_name):
            continue
        else:
            ERROR("File %s doesn't exist in %s" % (file_name, path))


def allocate_files_per_jobs(args):
    if args.net_config_slave is not None:
        files = manul_network.get_files_list_from_master(args.net_config_slave, args.nfuzzers) # ask master to provide list of files
        check_if_exist(files, args.input)
        return split_files_by_count(files, args.nfuzzers)

    files_list = get_files_list(args.input)

    if args.net_config_master is not None:

        ips = manul_network.get_slaves_ips(args.net_config_master)
        slaves, total_threads_count = manul_network.get_remote_threads_count(ips) # ask slaves count and threads
        total_threads_count += args.nfuzzers

        files = split_files_by_count(files_list, total_threads_count)
        piece_id = 0
        for ip, port, slave_threads_count in slaves:
            manul_network.send_files_list(ip, port, files[piece_id:slave_threads_count + piece_id]) #  send them files list
            piece_id += slave_threads_count
        files = files[piece_id:]
    else:
        files = split_files_by_count(files_list, args.nfuzzers)

    return files


def parse_args():
    parser = argparse.ArgumentParser(prog = "manul.py",
                                     description = 'Manul - coverage-guided parallel fuzzing for native applications.',
                                     usage = '%(prog)s -i /home/user/inputs_dir -o /home/user/outputs_dir -n 40 "pdftocairo -png @@"')
    requiredNamed = parser.add_argument_group('Required parameters')
    requiredNamed.add_argument('-i', required=True, dest='input', help = "Path to directory with initial corpus")
    requiredNamed.add_argument('-o', dest='output', required=True, default="manul_output",
                               help = "Path to output directory")


    parser.add_argument('-n', default=1, type=int, dest='nfuzzers', help = "Number of parallel fuzzers")
    parser.add_argument('-s', default=False, action='store_true', dest="simple_mode",
                        help = "Run dumb fuzzing (no code instrumentation)")
    parser.add_argument('-c', default="manul.config", dest = "config",
                        help = "Path to config file with additional options (see manul.config)")
    parser.add_argument('-r', default=False, action='store_true', dest = "restore", help = "Restore previous session")

    # this options should be specified through config file and hidden
    parser.add_argument('--determinstic_seed', default=False, action='store_true', help = argparse.SUPPRESS)
    parser.add_argument('--print_per_thread', default=False, action='store_true', dest="threads_info", help = argparse.SUPPRESS)
    parser.add_argument('--dbi', default = None, help = argparse.SUPPRESS)
    parser.add_argument('--dbi_root', help = argparse.SUPPRESS)
    parser.add_argument('--dbi_client_root', help = argparse.SUPPRESS)
    parser.add_argument('--dbi_client_libs', help = argparse.SUPPRESS)
    parser.add_argument('--timeout', default=10, type=int, help = argparse.SUPPRESS)
    parser.add_argument('--net_config_master', help = argparse.SUPPRESS)
    parser.add_argument('--net_config_slave', help = argparse.SUPPRESS)
    parser.add_argument('--debug', default=False, action='store_true', help = argparse.SUPPRESS)
    parser.add_argument('--manul_logo', default=False, action='store_true', help = argparse.SUPPRESS)
    parser.add_argument('--logging_enable', default=False, action='store_true', help = argparse.SUPPRESS)
    parser.add_argument('--sync_freq', default=1000000, type=int, help = argparse.SUPPRESS)
    parser.add_argument('--cmd_fuzzing', default=False, action='store_true', help = argparse.SUPPRESS)
    parser.add_argument('--target_ip_port', default = None, help = argparse.SUPPRESS)
    parser.add_argument('--target_protocol', default = None, help = argparse.SUPPRESS)
    parser.add_argument('--mutator_weights', default = None, help = argparse.SUPPRESS)
    parser.add_argument('--user_signals', default = None, help = argparse.SUPPRESS)
    parser.add_argument("--dict", default = None, help = argparse.SUPPRESS)

    parser.add_argument('target_binary', nargs='*', help="The target binary and options to be executed.")

    args = parser.parse_args()
    additional_args = parse_config(args.config)
    # A little hack here. We actually adding commands from config to cmd string and parse it all together.
    final_cmd_to_parse = "%s %s" % (" ".join(sys.argv[1:-1]), additional_args)

    final_cmd_to_parse = final_cmd_to_parse.split(" ")
    final_cmd_to_parse.append("%s" % sys.argv[-1])

    args = parser.parse_args(final_cmd_to_parse)

    if args.manul_logo:
        printing.print_logo()

    if "@@" not in args.target_binary[0]:
        ERROR("Your forgot to specify @@ for your target. Call manul.py -h for more details")
    modes_count = 0
    if args.simple_mode and args.dbi is not None:
        ERROR("Options mismatch. Simple mode can't be executed with DBI mode together (check manul.config).")

    if args.restore and not args.simple_mode:
        ERROR("Session restore for coverage-guided mode is not yet supported")

    if not args.mutator_weights:
        ERROR("At least one mutator should be specified")

    if (args.target_ip_port and not args.target_protocol) or (args.target_protocol and not args.target_ip_port):
        ERROR("Both target_ip_port and target_protocol should be specified")
    if args.target_protocol and args.target_protocol != "tcp" and args.target_protocol != "udp":
        ERROR("Invalid protocol. Should be tcp or udp.")
    if args.target_ip_port and args.nfuzzers > 1:
        ERROR("Multi-threaded network fuzzing is not supported, yet")

    if args.dict:
        if not os.path.isfile(args.dict):
            WARNING(None, "Unable to read dictionary file from %s, file doesn't exist" % args.dict)

    return args


if __name__ == "__main__":
    start = timer()
    args = parse_args()

    printing.DEBUG_PRINT = args.debug

    binary_to_check = args.target_binary[0]
    target_binary = binary_to_check.split(" ")[0] # here we assume that our path to binary doesn't have spaces

    dbi_setup = None
    if args.dbi is not None:
        dbi_setup = configure_dbi(args, target_binary, args.debug)

    check_binary(target_binary) # check if our binary exists and is actually instrumented

    if not args.simple_mode and args.dbi is None and not check_instrumentation(target_binary):
        ERROR("Failed to find afl's instrumentation in the target binary, try to recompile or run manul in dumb mode")

    if not os.path.isdir(args.input):
        ERROR("Input directory doesn't exist")

    if not os.path.isdir(args.output):
        ERROR("Output directory doesn't exist")

    if args.output.endswith('/'):
        args.output = args.output[:-1]
    if args.input.endswith('/'):
        args.input = args.input[:-1]

    if not args.restore and os.listdir(args.output):
        WARNING(None, "Output directory is not empty, creating backup of output folder")
        id = get_available_id_for_backup(args.output)
        os.rename(args.output, args.output + "_%d" % id)
        os.mkdir(args.output)
        INFO(0, None, None, "Done")

    #if radamsa weight is not zero, check that we can actually execute it
    if "radamsa:0" not in args.mutator_weights:
        if sys.platform == "win32":
            check_binary("radamsa.exe")
        else:
            check_binary("radamsa")


    files = allocate_files_per_jobs(args)

    virgin_bits = None
    crash_bits = None
    if not args.simple_mode:
        virgin_bits = multiprocessing.Array("i", SHM_SIZE)
        crash_bits = multiprocessing.Array("i", SHM_SIZE)
        for i in range(0, SHM_SIZE):
            virgin_bits[i] = 255 # initalizing with all 0xFFs
            crash_bits[i] = 255

    # allocating data structures where we store all statistics about our fuzzers
    stats = FuzzerStats()
    all_threads_stats = list()
    all_threads_handles = list()

    for i, files_piece in enumerate(files):
        stats_array = multiprocessing.Array("d", stats.get_len())
        t = multiprocessing.Process(target=run_fuzzer_instance, args=(files_piece, i, virgin_bits, args, stats_array,
                                                                      args.restore, crash_bits, dbi_setup))
        t.start()
        all_threads_stats.append(stats_array)
        #all_threads_handles.append(t)

    INFO(0, None, None, "%d fuzzer instances sucessfully launched" % args.nfuzzers)

    sync_t = None
    if (args.net_config_slave is not None or args.net_config_master is not None) and not args.simple_mode:
        INFO(1, None, None, "Allocating special thread for bitmap syncronization")
        ips = None
        if args.net_config_master is not None:
            ips = manul_network.get_slaves_ips(args.net_config_master)
            sync_t = threading.Thread(target=manul_network.sync_remote_bitmaps,
                                      args=(virgin_bits, ips))
        elif args.net_config_slave is not None:
            sync_t = threading.Thread(target=manul_network.receive_bitmap_slave,
                                      args=(args.net_config_slave, virgin_bits))
        sync_t.setDaemon(True)
        sync_t.start()

    watchdog_t = threading.Thread(target=watchdog, args=(args.timeout,))
    watchdog_t.setDaemon(True)
    watchdog_t.start()

    try:
        while True:
            threads_inactive = 0
            for i, t in enumerate(all_threads_handles):
                if not t.is_alive():
                    threads_inactive += 1
                    WARNING(None, "Fuzzer %d unexpectedly terminated" % i)
                    if not args.simple_mode:
                        continue
                    WARNING(None, "Trying to restore %d fuzzer" % i)
                    files_list = files[i]
                    stats_array = all_threads_stats[i]
                    t = multiprocessing.Process(target=run_fuzzer_instance,  # TODO: try to restore fuzzing session
                                                args=(files_list, i, virgin_bits, args, stats_array, True,
                                                      crash_bits, dbi_setup))
                    t.setDaemon(True)
                    t.start()
                    all_threads_handles[i] = t

            if sync_t is not None and not sync_t.alive():
                WARNING(None, "Syncronization thread is not alive")

            end = timer() - start

            bytes_cov = 0.0
            if not args.simple_mode:
                bytes_cov = get_bytes_covered(virgin_bits)
            active_threads_count = len(all_threads_handles) - threads_inactive
            # printing statistics
            if args.threads_info:
                printing.print_per_thread(all_threads_stats, bytes_cov, end, active_threads_count, args, args.mutator_weights)
            else:
                printing.print_summary(all_threads_stats, bytes_cov, end, active_threads_count, args, UPDATE, args.mutator_weights)

            time.sleep(STATS_FREQUENCY)
    except (KeyboardInterrupt, SystemExit):
        INFO(0, None, None, "Stopping all fuzzers and threads")
        kill_all()
        INFO(0, None, None, "Stopped, exiting")
        sys.exit()
