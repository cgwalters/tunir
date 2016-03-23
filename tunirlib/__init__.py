import os
import sys
import json
import time
import redis
import signal
import argparse
import tempfile
import shutil
import codecs
import paramiko
import socket
from pprint import pprint
from testvm import build_and_run
from tunirvagrant import vagrant_and_run
from tuniraws import aws_and_run
from tunirdocker import Docker, Result
from tunirmultihost import start_multihost
from tunirutils import run
from collections import OrderedDict

STR = OrderedDict()


def read_job_configuration(jobname='', config_dir='./'):
    """
    :param jobname: Name of the job
    :param config_dir: Directory for configuration.
    :return: Configuration dict
    """
    data = None
    name = jobname + '.json'
    name = os.path.join(config_dir, name)
    if not os.path.exists(name):
        print "Job configuration is missing."
        return None
    with open(name) as fobj:
        data = json.load(fobj)
    return data

def try_again(func):
    "We will try again for ssh errors."
    def wrapper(*args, **kargs):
        try:
            result = func(*args, **kargs)
        except paramiko.ssh_exception.SSHException:
            print "Getting ssh exception, sleeping for 30 seconds and then trying again."
            time.sleep(30)
            print "Now trying for second time."
            result = func(*args, **kargs)
        return result
    return wrapper

@try_again
def execute(config, command, container=None):
    """
    Executes a given command based on the system.
    :param config: Configuration dictionary.
    :param command: The command to execute
    :return: (Output text, string)
    """
    result = ''
    negative = 'no'
    if command.startswith('@@'):
        command = command[3:].strip()
        result = run(config['host_string'], config.get('port', '22'), config['user'],
                         config.get('password', None), command, key_filename=config.get('key', None),
                         timeout=config.get('timeout', 600))
        if result.return_code != 0:  # If the command does not fail, then it is a failure.
            negative = 'yes'
    elif command.startswith('##'):
        command = command[3:].strip()
        result = run(config['host_string'], config.get('port', '22'), config['user'],
                         config.get('password', None), command, key_filename=config.get('key', None),
                         timeout=config.get('timeout', 600))
        negative = 'dontcare'
    else:
        result = run(config['host_string'], config.get('port', '22'), config['user'],
                         config.get('password', None), command, key_filename=config.get('key', None),
                         timeout=config.get('timeout', 600))
    return result, negative

def update_result(result, command, negative):
    """
    Updates the result based on input.

    :param result: Output from the command
    :param job: Job object from model.
    :param command: Text command.
    :param negative: If it is a negative command, which is supposed to fail.

    :return: Boolean, False if the job as whole is failed.
    """
    status = True
    if negative == 'yes':
        if result.return_code == 0:
            status = False
    else:
        if result.return_code != 0:
            status = False

    d = {'command': command, 'result': unicode(result, encoding='utf-8', errors='replace'),
         'ret': result.return_code, 'status': status}
    STR[command] = d


    if result.return_code != 0 and negative == 'no':
        # Save the error message and status as fail.
        return False

    return True


def run_job(args, jobpath, job_name='', config=None, container=None,
            port=None ):
    """
    Runs the given command using fabric.

    :param args: Command line arguments.
    :param jobpath: Path to the job file.
    :param job_name: string job name.
    :param config: Configuration of the given job
    :param container: Docker object for a Docker job.
    :param port: The port number to connect in case of a vm.
    :return: Status of the job in boolean
    """
    if not os.path.exists(jobpath):
        print "Missing job file {0}".format(jobpath)
        return False

    # Now read the commands inside the job file
    # and execute them one by one, we need to save
    # the result too.
    commands = []
    status = True
    timeout_issue = False
    ssh_issue = False

    result_path = config.get('result_path', '/var/run/tunir/tunir_result.txt')

    with open(jobpath) as fobj:
        commands = fobj.readlines()


    try:
        job = None

        print "Starting a stateless job."

        if not 'host_string' in config: # For VM based tests.
            config['host_string'] = '127.0.0.1'

        if config['type'] == 'vm':
            config['port'] = port
        elif config['type'] == 'bare':
            config['host_string'] = config['image']
        elif config['type'] == 'docker':
            # Now we will convert this job as a bare metal :)
            config['type'] = 'bare'
            config['host_string'] = container.ip
            time.sleep(10)
        for command in commands:
            negative = False
            result = ''
            command = command.strip('\n')
            if command.startswith('SLEEP'): # We will have to sleep
                word = command.split(' ')[1]
                print "Sleeping for %s." % word
                time.sleep(int(word))
                continue
            print "Executing command: %s" % command

            try:
                result, negative = execute(config, command)
                status = update_result(result, command, negative)
                if not status:
                    break
            except socket.timeout: # We have a timeout in the command
                status = False
                timeout_issue = True
                break
            except paramiko.ssh_exception.SSHException:
                status = False
                ssh_issue = True
                break
            except Exception as err: #execute failed for some reason, we don't know why
                status = False
                print err
                break

        # If we are here, that means all commands ran successfully.

    finally:
        # Now for stateless jobs
        print "\n\nJob status: %s\n\n" % status
        nongating = {'number':0, 'pass':0, 'fail':0}

        with codecs.open(result_path, 'w', encoding='utf-8') as fobj:
            for key, value in STR.iteritems():
                fobj.write("command: %s\n" % value['command'])
                print "command: %s" % value['command']
                if value['command'].startswith('##'):
                    nongating['number'] += 1
                    if value['status'] == False:
                        nongating['fail'] += 1
                    else:
                        nongating['pass'] += 1
                fobj.write("status: %s\n" % value['status'])
                print "status: %s\n" % value['status']
                fobj.write(value['result'])
                print value['result']
                fobj.write("\n")
                print "\n"
            if timeout_issue: # We have 10 minutes timeout in the last command.
                msg = "Error: We have socket timeout in the last command."
                fobj.write(msg)
                print msg
            if ssh_issue: # We have 10 minutes timeout in the last command.
                msg = "Error: SSH into the system failed."
                fobj.write(msg)
                print msg
            fobj.write("\n\n")
            print "\n\n"
            msg = """Non gating tests status:
Total:{0}
Passed:{1}
Failed:{2}""".format(nongating['number'], nongating['pass'],
                nongating['fail'])
            fobj.write(msg)
            print msg
        return status


def main(args):
    "Starting point of the code"
    job_name = ''
    vm = None
    node = None
    port = None
    temp_d = None
    container = None
    atomic = False
    debug = False
    image_dir = ''
    vagrant = None
    return_code = -100
    run_job_flag = True

    if args.atomic:
        atomic = True
    if args.debug:
        debug = True
    # For multihost
    if args.multi:
        jobpath = os.path.join(args.config_dir, args.multi + '.txt')
        start_multihost(args.multi, jobpath, debug)
        os.system('stty sane')
        return
    if args.job:
        job_name = args.job
    else:
        sys.exit(-2)

    jobpath = os.path.join(args.config_dir, job_name + '.txt')


    # First let us read the vm configuration.
    config = read_job_configuration(job_name, args.config_dir)
    if not config: # Bad config name
        sys.exit(-1)

    os.system('mkdir -p /var/run/tunir')
    if config['type'] == 'vm':
        status = start_multihost(job_name, jobpath, debug, config)
        os.system('stty sane')
        sys.exit(status)

    if config['type'] == 'docker':
        container = Docker(config['image'])

    if config['type'] == 'vagrant':
        vagrant, config = vagrant_and_run(config)
        if vagrant.failed:
            run_job_flag = False

    elif config['type'] == 'aws':
        node, config = aws_and_run(config)
        if node.failed:
            run_job_flag = False
        else:
            print "We have an instance ready in AWS.", node.node

    try:
        if run_job_flag:
            status = run_job(args, jobpath, job_name, config, container, port)
            if status:
                return_code = 0
    finally:
        # Now let us kill the kvm process
        if vagrant:
            print "Removing the box."
            vagrant.destroy()
        elif node:
            node.destroy()
            #print "Not destorying the node", node

        sys.exit(return_code)


def startpoint():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", help="The job configuration name to run")
    parser.add_argument("--stateless", help="Do not store the result, just print it in the STDOUT.", action='store_true')
    parser.add_argument("--config-dir", help="Path to the directory where the job config and commands can be found.",
                        default='./')
    parser.add_argument("--image-dir", help="Path to the directory where vm images will be held")
    parser.add_argument("--atomic", help="We are using an Atomic image.", action='store_true')
    parser.add_argument("--debug", help="Keep the vms running for debug in multihost mode.", action='store_true')
    parser.add_argument("--multi", help="The multihost configuration")
    args = parser.parse_args()

    main(args)

if __name__ == '__main__':
    startpoint()
