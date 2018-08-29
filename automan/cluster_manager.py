"""Code to bootstrap and update the project so a remote host can be used as a
worker to help with the automation of tasks.

This requires ssh/scp and rsync to work on all machines.

This is currently only tested on Linux machines.

"""

import json
import os
import shlex
import stat
import subprocess
import sys
from textwrap import dedent

try:
    from urllib import urlopen
except ImportError:
    from urllib.request import urlopen


def prompt(msg):
    try:
        return raw_input(msg)
    except NameError:
        return input(msg)


class ClusterManager(object):
    """The cluster manager class.

    This class primarily helps setup software on a remote worker machine such
    that it can run any computational jobs from the automation framework.

    The general directory structure of a remote worker machine is as follows::

        remote_home/           # Could be ~
            automan/           # Root of automation directory (configurable)
                envs/          # python virtual environments for use.
                my_project/    # Current directory for specific projects.

    The project directories are synced from this machine to the remote worker.

    The "my_project" is the root of the directory with the automation script
    and this should contain the required sources that need to be executed. One
    can use a list of source directories which will be copied over but it is
    probably most convenient to put it all in the root of the project directory
    to keep everything self-contained.

    The `ClusterManager` class manages these remote workers by helping setup
    the directories, bootstrapping the Python virtualenv and also keeping these
    up-to-date as project directory is changed on the local machine.

    The class therefore has two primary public methods,

    1. `add_worker(self, host, home, nfs)` which adds a new worker machine by
       bootstrapping the machine with the software and the appropriate source
       directories.

    2. `update()`, which keeps the directory and software up-to-date.

    The class variables BOOTSTRAP and UPDATE are the content of scripts
    uploaded to these machines and should be extended by users to do what they
    wish.

    The class creates a ``config.json`` in the current working directory that
    may be edited by a user. It also creates a directory called
    ``.{self.root}`` which defaults to ``.automan``. The bootstrap and
    update scripts are put here and may be edited by the user for any new
    hosts.

    """

    #######################################################
    # These scripts are used to bootstrap the installation
    # and update them.
    BOOTSTRAP = dedent("""\
        #!/bin/bash

        set -e
        if hash virtualenv 2>/dev/null; then
            virtualenv --system-site-packages envs/{project_name}
        else
            python virtualenv.py --system-site-packages envs/{project_name}
        fi
        source envs/{project_name}/bin/activate

        pip install automan

        # Run any requirements.txt from the user
        cd {project_name}
        if [ -f "requirements.txt" ] ; then
            pip install -r requirements.txt
        fi
        """)

    UPDATE = dedent("""\
         #!/bin/bash

         set -e
         source envs/{project_name}/bin/activate
         # Run any requirements.txt from the user
         cd {project_name}
         if [ -f "requirements.txt" ] ; then
             pip install -r requirements.txt
         fi
         """)
    #######################################################

    def __init__(self, root='automan', sources=None,
                 config_fname='config.json', exclude_paths=None):
        """Create a cluster manager instance.

        **Parameters**

        root: str
           The name of the root directory where all the files on the remote
           will be created.
        sources: list
           A list of source directories to sync.
        config_fname: str
           The name of the config file to create.
        exclude_paths: list
           A list of paths to exclude while syncing. This is in a form suitable
           to pass to rsync.
        """
        self.root = root
        self.workers = []
        self.sources = sources
        self.scripts_dir = os.path.abspath('.' + self.root)
        self.exclude_paths = exclude_paths if exclude_paths else []

        # This is setup by the config and is the name of
        # the project directory.
        self.project_name = None

        # The config file will always trump any direct settings
        # unless there is no config file.
        self.config_fname = config_fname
        self._read_config()
        if not os.path.exists(self.scripts_dir):
            os.makedirs(self.scripts_dir)

    # ### Private Protocol ########################################

    def _bootstrap(self, host, home):
        venv_script = self._get_virtualenv()

        cmd = ("ssh {host} 'cd {home}; mkdir -p {root}/envs'; " +
               "mkdir -p {root}/{project_name}/.{root}").format(
                   home=home, host=host, root=self.root,
                   project_name=self.project_name
               )
        self._run_command(cmd)

        root = os.path.join(home, self.root)
        cmd = "scp {venv_script} {host}:{root}".format(
            host=host, root=root, venv_script=venv_script
        )
        self._run_command(cmd)

        self._update_sources(host, home)

        cmd = "ssh {host} 'cd {root}; ./{project_name}/.{root}/bootstrap.sh'"
        cmd = cmd.format(host=host, root=root, project_name=self.project_name)
        try:
            self._run_command(cmd)
        except subprocess.CalledProcessError:
            msg = dedent("""
            ******************************************************************
            Bootstrapping of remote host {host} failed.
            All files have been copied to the host.

            Please take a look at
               {root}/{project_name}/.{root}/bootstrap.sh
            and try to fix it.

            You should run it from within the {root} directory as:

               ./{project_name}/.{root}/bootstrap.sh

            Once the bootstrap.sh script runs successfully, the worker can be
            used without any further steps.

            The default bootstrap script is in
                {scripts_dir}
            and can be edited by you. These will be used for any new hosts
            you add.
            ******************************************************************
            """.format(root=root, host=host, scripts_dir=self.scripts_dir,
                       project_name=self.project_name)
            )
            print(msg)
        else:
            print("Bootstrapping {host} succeeded!".format(host=host))

    def _get_virtualenv(self):
        script = os.path.join(self.scripts_dir, 'virtualenv.py')
        if not os.path.exists(script):
            print("Downloading latest virtualenv.py")
            url = 'https://raw.githubusercontent.com/pypa/virtualenv/master/virtualenv.py'
            opener = urlopen(url)
            with open(script, 'wb') as f:
                f.write(opener.read())
        return script

    def _read_config(self):
        if os.path.exists(self.config_fname):
            with open(self.config_fname) as f:
                data = json.load(f)
            self.root = data['root']
            self.project_name = data['project_name']
            self.sources = data['sources']
            self.workers = data['workers']
        else:
            if self.sources is None or len(self.sources) == 0:
                project_dir = os.path.abspath(os.getcwd())
                self.project_name = os.path.basename(project_dir)
                self.sources = [project_dir]
            self.workers = [dict(host='localhost', home='', nfs=False)]
            self._write_config()
        self.scripts_dir = os.path.abspath('.' + self.root)

    def _rebuild(self, host, home):
        root = os.path.join(home, self.root)
        command = "ssh {host} 'cd {root}; ./{project_name}/.{root}/update.sh'"
        command = command.format(host=host, root=root,
                                 project_name=self.project_name)
        self._run_command(command)

    def _run_command(self, cmd, **kw):
        print(cmd)
        subprocess.check_call(shlex.split(cmd), **kw)

    def _sync_dir(self, host, src, dest):
        options = ""
        kwargs = dict()
        if os.path.isdir(os.path.join(src, '.git')):
            exclude = 'git -C {src} ls-files --exclude-standard -oi '\
                      '--directory '.format(src=src)
            options = '--exclude-from=-'
            proc = subprocess.Popen(
                shlex.split(exclude),
                stdout=subprocess.PIPE
            )
            kwargs['stdin'] = proc.stdout
        if self.exclude_paths:
            for path in self.exclude_paths:
                options += ' --exclude="%s"' % path

        command = "rsync -a {options} {src} {host}:{dest} ".format(
            options=options, src=src, host=host, dest=dest
        )
        self._run_command(command, **kwargs)

    def _update_sources(self, host, home):
        for local_dir in self.sources:
            remote_dir = os.path.join(home, self.root + '/')
            self._sync_dir(host, local_dir, remote_dir)

        scripts_dir = self.scripts_dir
        bootstrap_code = self.BOOTSTRAP.format(project_name=self.project_name)
        update_code = self.UPDATE.format(project_name=self.project_name)
        scripts = {'bootstrap.sh': bootstrap_code,
                   'update.sh': update_code}
        for script, code in scripts.items():
            fname = os.path.join(scripts_dir, script)
            if not os.path.exists(fname):
                # Create the scripts if they don't exist.
                with open(fname, 'w') as f:
                    f.write(code)

        script_files = [os.path.join(scripts_dir, x) for x in scripts]
        for fname in script_files:
            mode = os.stat(fname).st_mode
            os.chmod(fname, mode | stat.S_IXUSR | stat.S_IXGRP)

        path = os.path.join(home, self.root, self.project_name,
                            '.' + self.root)
        cmd = "scp {script_files} {host}:{path}".format(
            host=host, path=path, script_files=' '.join(script_files)
        )
        self._run_command(cmd)

    def _write_config(self):
        print("Writing %s" % self.config_fname)
        data = dict(
            project_name=self.project_name,
            root=self.root,
            sources=self.sources,
            workers=self.workers
        )
        with open(self.config_fname, 'w') as f:
            json.dump(data, f, indent=2)

    # ### Public Protocol ########################################

    def add_worker(self, host, home, nfs):
        if host == 'localhost':
            self.workers.append(dict(host=host, home=home, nfs=nfs))
        else:
            root = self.root
            curdir = os.path.basename(os.getcwd())
            if nfs:
                python = sys.executable
                chdir = curdir
            else:
                python = os.path.join(
                    home, root,
                    'envs/{project_name}/bin/python'.format(
                        project_name=self.project_name
                    )
                )
                chdir = os.path.join(home, root, curdir)
            self.workers.append(
                dict(host=host, home=home, nfs=nfs, python=python, chdir=chdir)
            )

        self._write_config()
        if host != 'localhost' and not nfs:
            self._bootstrap(host, home)

    def update(self, rebuild=True):
        for worker in self.workers:
            host = worker.get('host')
            home = worker.get('home')
            nfs = worker.get('nfs', False)
            if host != 'localhost' and not nfs:
                self._update_sources(host, home)
                if rebuild:
                    self._rebuild(host, home)

    def create_scheduler(self):
        """Return a `automan.jobs.Scheduler` from the configuration.
        """
        from .jobs import Scheduler

        scheduler = Scheduler(root='.')
        for worker in self.workers:
            host = worker.get('host')
            nfs = worker.get('nfs', False)
            if host == 'localhost':
                scheduler.add_worker(dict(host='localhost'))
            else:
                python = worker.get('python')
                chdir = worker.get('chdir')
                scheduler.add_worker(
                    dict(host=host, python=python, chdir=chdir, nfs=nfs)
                )
        return scheduler

    def cli(self, argv=None):
        """This is just a demonstration of how this class could be used.
        """
        import argparse
        parser = argparse.ArgumentParser(description='Setup remote workers.')

        parser.add_argument(
            '-a', '--add-node', action="store", dest="host", type=str,
            default='', help="Add a new remote worker."
        )
        parser.add_argument(
            '--home', action="store", dest="home", type=str,
            default='',
            help='Home directory of the remote worker (to be used with -a)'
        )
        parser.add_argument(
            '--nfs', action="store_true", dest="nfs",
            default=False,
            help=('Does the remote remote worker share the filesystem '
                  '(to be used with -a)')
        )
        parser.add_argument(
            '--no-rebuild', action="store_true", dest="no_rebuild",
            default=False, help="Do not rebuild the sources on sync."
        )

        args = parser.parse_args(argv)

        if len(args.host) == 0:
            self.update(not args.no_rebuild)
        else:
            self.add_worker(args.host, args.home, args.nfs)
