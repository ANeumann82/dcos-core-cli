import ast
import contextlib
import json
import os
import re
import sys
import threading

from datetime import timedelta
import pytest
import retrying

from six.moves.BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

from dcos import constants

from dcoscli.test.common import (assert_command, assert_lines, exec_command,
                                 popen_tty, update_config)
from dcoscli.test.marathon import (app, list_apps, list_deployments, show_app,
                                   start_app, watch_all_deployments,
                                   watch_deployment)


_ZERO_INSTANCE_APP_ID = 'zero-instance-app'
_ZERO_INSTANCE_APP_INSTANCES = 100


def test_help():
    with open('dcoscli/data/help/marathon.txt') as content:
        assert_command(['dcos', 'marathon', '--help'],
                       stdout=content.read().encode('utf-8'))


def test_version():
    assert_command(['dcos', 'marathon', '--version'],
                   stdout=b'dcos-marathon version SNAPSHOT\n')


def test_info():
    assert_command(['dcos', 'marathon', '--info'],
                   stdout=b'Deploy and manage applications to DC/OS\n')


def test_about():
    returncode, stdout, stderr = exec_command(['dcos', 'marathon', 'about'])

    assert returncode == 0
    assert stderr == b''

    result = json.loads(stdout.decode('utf-8'))
    assert result['name'] == "marathon"


@pytest.fixture
def env():
    r = os.environ.copy()
    r.update({constants.PATH_ENV: os.environ[constants.PATH_ENV]})

    return r


def test_empty_list():
    list_apps()


def test_add_app_through_http():
    with _zero_instance_app_through_http():
        list_apps('zero-instance-app')


def test_add_app_bad_resource():
    stderr = (b'Can\'t read from resource: bad_resource.\n'
              b'Please check that it exists.\n')
    assert_command(['dcos', 'marathon', 'app', 'add', 'bad_resource'],
                   returncode=1,
                   stderr=stderr)


def test_remove_app():
    with _zero_instance_app():
        pass
    list_apps()


def test_add_bad_json_app():
    with open('tests/data/marathon/apps/bad.json') as fd:
        returncode, stdout, stderr = exec_command(
            ['dcos', 'marathon', 'app', 'add'],
            stdin=fd)

        assert returncode == 1
        assert stdout == b''
        assert stderr.decode('utf-8').startswith('Error loading JSON: ')


def test_add_existing_app():
    with _zero_instance_app():
        app_path = 'tests/data/marathon/apps/zero_instance_sleep_v2.json'
        with open(app_path) as fd:
            stderr = b"Application '/zero-instance-app' already exists\n"
            assert_command(['dcos', 'marathon', 'app', 'add'],
                           returncode=1,
                           stderr=stderr,
                           stdin=fd)


def test_show_absolute_app_version():
    with _zero_instance_app():
        _update_app(
            'zero-instance-app',
            'tests/data/marathon/apps/update_zero_instance_sleep.json')

        result = show_app('zero-instance-app')
        show_app('zero-instance-app', result['version'])


def test_show_relative_app_version():
    with _zero_instance_app():
        _update_app(
            'zero-instance-app',
            'tests/data/marathon/apps/update_zero_instance_sleep.json')
        show_app('zero-instance-app', "-1")


def test_show_missing_relative_app_version():
    app_id = _ZERO_INSTANCE_APP_ID

    with _zero_instance_app():
        _update_app(
            app_id,
            'tests/data/marathon/apps/update_zero_instance_sleep.json')

        # Marathon persists app versions indefinitely by ID, so pick a large
        # index here in case the history is long
        cmd = ['dcos', 'marathon', 'app', 'show', '--app-version=-200', app_id]
        returncode, stdout, stderr = exec_command(cmd)

        assert returncode == 1
        assert stdout == b''

        pattern = ("Application 'zero-instance-app' only has [1-9][0-9]* "
                   "version\\(s\\)\\.\n")
        assert re.fullmatch(pattern, stderr.decode('utf-8'), flags=re.DOTALL)


def test_show_missing_absolute_app_version():
    with _zero_instance_app():
        _update_app(
            'zero-instance-app',
            'tests/data/marathon/apps/update_zero_instance_sleep.json')

        returncode, stdout, stderr = exec_command(
            ['dcos', 'marathon', 'app', 'show',
             '--app-version=2000-02-11T20:39:32.972Z', 'zero-instance-app'])

        assert returncode == 1
        assert stdout == b''
        assert stderr.decode('utf-8').startswith(
            "Error: App '/zero-instance-app' does not exist")


def test_show_bad_app_version():
    with _zero_instance_app():
        _update_app(
            'zero-instance-app',
            'tests/data/marathon/apps/update_zero_instance_sleep.json')

        returncode, stdout, stderr = exec_command(
            ['dcos', 'marathon', 'app', 'show', '--app-version=20:39:32.972Z',
             'zero-instance-app'])
        assert returncode == 1
        assert stdout == b''
        assert stderr.startswith(b'Error while fetching')
        pattern = (b"""{"message":"Invalid timestamp provided """
                   b"""\'20:39:32.972Z\'. Expecting ISO-8601 """
                   b"""datetime string."}".\n""")

        assert stderr.endswith(pattern)


def test_show_bad_relative_app_version():
    with _zero_instance_app():
        _update_app(
            'zero-instance-app',
            'tests/data/marathon/apps/update_zero_instance_sleep.json')

        assert_command(
            ['dcos', 'marathon', 'app', 'show',
             '--app-version=2', 'zero-instance-app'],
            returncode=1,
            stderr=b"Relative versions must be negative: 2\n")


def test_start_missing_app():
    assert_command(
        ['dcos', 'marathon', 'app', 'start', 'missing-id'],
        returncode=1,
        stderr=b"Error: App '/missing-id' does not exist\n")


def test_start_already_started_app():
    with _zero_instance_app():
        start_app('zero-instance-app')

        stdout = (b"Application 'zero-instance-app' already "
                  b"started: 1 instances.\n")
        assert_command(
            ['dcos', 'marathon', 'app', 'start', 'zero-instance-app'],
            returncode=1,
            stdout=stdout)


def test_stop_missing_app():
    assert_command(['dcos', 'marathon', 'app', 'stop', 'missing-id'],
                   returncode=1,
                   stderr=b"Error: App '/missing-id' does not exist\n")


def test_stop_app():
    with _zero_instance_app():
        start_app('zero-instance-app', 3)
        watch_all_deployments()

        returncode, stdout, stderr = exec_command(
            ['dcos', 'marathon', 'app', 'stop', 'zero-instance-app'])

        assert returncode == 0
        assert stdout.decode().startswith('Created deployment ')
        assert stderr == b''


def test_stop_already_stopped_app():
    with _zero_instance_app():
        stdout = (b"Application 'zero-instance-app' already "
                  b"stopped: 0 instances.\n")
        assert_command(
            ['dcos', 'marathon', 'app', 'stop', 'zero-instance-app'],
            returncode=1,
            stdout=stdout)


def test_update_missing_app():
    assert_command(['dcos', 'marathon', 'app', 'update', 'missing-id'],
                   stderr=b"Error: App '/missing-id' does not exist\n",
                   returncode=1)


def test_update_bad_type():
    with _zero_instance_app():
        returncode, stdout, stderr = exec_command(
            ['dcos', 'marathon', 'app', 'update',
             'zero-instance-app', 'cpus="a string"'])

        stderr_end = b"""{"message":"Invalid JSON","details":[{"path":"/cpus","errors":["error.expected.jsnumber"]}]}"""  # noqa: E501

        assert returncode == 1
        assert stderr_end in stderr
        assert stdout == b''


def test_update_invalid_request():
    returncode, stdout, stderr = exec_command(
        ['dcos', 'marathon', 'app', 'update', '{', 'instances'])
    assert returncode == 1
    assert stdout == b''
    stderr = stderr.decode()
    # TODO (tamar): this becomes 'Error: App '/{' does not exist\n"'
    # in Marathon 0.11.0
    assert stderr.startswith('Error on request')
    assert stderr.endswith('HTTP 400: Bad Request\n')


def test_app_add_invalid_request():
    path = os.path.join(
        'tests', 'data', 'marathon', 'apps', 'app_add_400.json')

    returncode, stdout, stderr = exec_command(
        ['dcos', 'marathon', 'app', 'add', path])

    stderr_end = b"""{"message":"Invalid JSON","details":[{"path":"/container/docker/network","errors":["error.unknown.enum.literal"]}]}"""  # noqa: E501

    assert returncode == 1
    assert stderr_end in stderr
    assert stdout == b''


def test_update_app():
    with _zero_instance_app():
        returncode, stdout, stderr = exec_command(
            ['dcos', 'marathon', 'app', 'update', 'zero-instance-app',
             'cpus=1', 'mem=20', "cmd='sleep 100'"])

        assert returncode == 0
        assert stdout.decode().startswith('Created deployment ')
        assert stderr == b''


def test_update_app_json():
    with _zero_instance_app():
        returncode, stdout, stderr = exec_command(
            ['dcos', 'marathon', 'app', 'update', 'zero-instance-app',
             "env='{\"key\":\"/value\"}'"])

        assert returncode == 0
        assert stdout.decode().startswith('Created deployment ')
        assert stderr == b''


def test_update_app_from_stdin():
    with _zero_instance_app():
        _update_app(
            'zero-instance-app',
            'tests/data/marathon/apps/update_zero_instance_sleep.json')


def test_restarting_stopped_app():
    with _zero_instance_app():
        stdout = (b"Unable to perform rolling restart of application '"
                  b"/zero-instance-app' because it has no running tasks\n")
        assert_command(
            ['dcos', 'marathon', 'app', 'restart', 'zero-instance-app'],
            returncode=1,
            stdout=stdout)


def test_restarting_missing_app():
    assert_command(['dcos', 'marathon', 'app', 'restart', 'missing-id'],
                   returncode=1,
                   stderr=b"Error: App '/missing-id' does not exist\n")


def test_killing_app():
    with _zero_instance_app():
        start_app('zero-instance-app', 3)
        watch_all_deployments()
        returncode, stdout, stderr = exec_command(
            ['dcos', 'marathon', 'app', 'kill', 'zero-instance-app'])
        assert returncode == 0
        assert stderr == b''
        out = stdout.decode()
        assert out.startswith('Killed tasks: ')
        out = out.strip('Killed tasks: ')
        dictout = ast.literal_eval(out)
        assert len(dictout) == 3


def test_killing_scaling_app():
    with _zero_instance_app():
        start_app('zero-instance-app', 3)
        watch_all_deployments()
        _list_tasks(3)
        command = ['dcos', 'marathon', 'app', 'kill', '--scale',
                   'zero-instance-app']
        returncode, stdout, stderr = exec_command(command)
        assert returncode == 0
        assert stdout.decode().startswith('Started deployment: ')
        assert stdout.decode().find('version') > -1
        assert stdout.decode().find('deploymentId') > -1
        assert stderr == b''
        watch_all_deployments()
        _list_tasks(0)


def test_killing_with_host_app():
    with _zero_instance_app():
        start_app('zero-instance-app', 3)
        watch_all_deployments()
        existing_tasks = _list_tasks(3, 'zero-instance-app')
        task_hosts = set([task['host'] for task in existing_tasks])
        if len(task_hosts) <= 1:
            pytest.skip('test needs 2 or more agents to succeed, '
                        'only {} agents available'.format(len(task_hosts)))
        assert len(task_hosts) > 1
        kill_host = list(task_hosts)[0]
        expected_to_be_killed = set([task['id']
                                     for task in existing_tasks
                                     if task['host'] == kill_host])
        not_to_be_killed = set([task['id']
                                for task in existing_tasks
                                if task['host'] != kill_host])
        assert len(not_to_be_killed) > 0
        assert len(expected_to_be_killed) > 0
        command = ['dcos', 'marathon', 'app', 'kill', '--host', kill_host,
                   'zero-instance-app']
        returncode, stdout, stderr = exec_command(command)
        assert stdout.decode().startswith('Killed tasks: ')
        assert stderr == b''
        new_tasks = set([task['id'] for task in _list_tasks()])
        assert not_to_be_killed.intersection(new_tasks) == not_to_be_killed
        assert len(expected_to_be_killed.intersection(new_tasks)) == 0


@pytest.mark.skipif(
    True, reason='https://github.com/mesosphere/marathon/issues/3251')
def test_kill_stopped_app():
    with _zero_instance_app():
        returncode, stdout, stderr = exec_command(
            ['dcos', 'marathon', 'app', 'kill', 'zero-instance-app'])
        assert returncode == 1
        assert stdout.decode().startswith('Killed tasks: []')


def test_kill_missing_app():
    returncode, stdout, stderr = exec_command(
        ['dcos', 'marathon', 'app', 'kill', 'app'])
    assert returncode == 1
    assert stdout.decode() == ''
    stderr_expected = "Error: App '/app' does not exist"
    assert stderr.decode().strip() == stderr_expected


def test_list_version_missing_app():
    assert_command(
        ['dcos', 'marathon', 'app', 'version', 'list', 'missing-id'],
        returncode=1,
        stderr=b"Error: App '/missing-id' does not exist\n")


def test_list_version_negative_max_count():
    assert_command(['dcos', 'marathon', 'app', 'version', 'list',
                    'missing-id', '--max-count=-1'],
                   returncode=1,
                   stderr=b'Maximum count must be a positive number: -1\n')


def test_list_version_app():
    app_id = _ZERO_INSTANCE_APP_ID

    with _zero_instance_app():
        _list_versions(app_id, 1)

        _update_app(
            app_id,
            'tests/data/marathon/apps/update_zero_instance_sleep.json')
        _list_versions(app_id, 2)


def test_list_version_max_count():
    app_id = _ZERO_INSTANCE_APP_ID

    with _zero_instance_app():
        _update_app(
            app_id,
            'tests/data/marathon/apps/update_zero_instance_sleep.json')

        _list_versions(app_id, 1, 1)
        _list_versions(app_id, 2, 2)
        _list_versions(app_id, 2, 3)


def test_list_empty_deployment():
    list_deployments(0)


def test_list_deployment():
    with _zero_instance_app():
        start_app('zero-instance-app', _ZERO_INSTANCE_APP_INSTANCES)
        list_deployments(1)


def test_list_deployment_table():
    """Simple sanity check for listing deployments with a table output.
    The more specific testing is done in unit tests.

    """

    with _zero_instance_app():
        start_app('zero-instance-app', _ZERO_INSTANCE_APP_INSTANCES)
        assert_lines(['dcos', 'marathon', 'deployment', 'list'], 2)


def test_list_deployment_missing_app():
    with _zero_instance_app():
        start_app('zero-instance-app')
        list_deployments(0, 'missing-id')


def test_list_deployment_app():
    with _zero_instance_app():
        start_app('zero-instance-app', _ZERO_INSTANCE_APP_INSTANCES)
        list_deployments(1, 'zero-instance-app')


def test_rollback_missing_deployment():
    assert_command(
        ['dcos', 'marathon', 'deployment', 'rollback', 'missing-deployment'],
        returncode=1,
        stderr=b'Error: DeploymentPlan missing-deployment does not exist\n')


def test_rollback_deployment():
    with _zero_instance_app():
        start_app('zero-instance-app', _ZERO_INSTANCE_APP_INSTANCES)
        result = list_deployments(1, 'zero-instance-app')

        returncode, stdout, stderr = exec_command(
            ['dcos', 'marathon', 'deployment', 'rollback', result[0]['id']])

        result = json.loads(stdout.decode('utf-8'))

        assert returncode == 0
        assert 'deploymentId' in result
        assert 'version' in result
        assert stderr == b''

        watch_all_deployments()
        list_deployments(0)


def test_stop_deployment():
    with _zero_instance_app():
        start_app('zero-instance-app', _ZERO_INSTANCE_APP_INSTANCES)
        result = list_deployments(1, 'zero-instance-app')

        assert_command(
            ['dcos', 'marathon', 'deployment', 'stop', result[0]['id']])

        list_deployments(0)


def test_watching_missing_deployment():
    watch_deployment('missing-deployment', 1)


def test_watching_deployment():
    with _zero_instance_app():
        start_app('zero-instance-app', _ZERO_INSTANCE_APP_INSTANCES)
        result = list_deployments(1, 'zero-instance-app')
        watch_deployment(result[0]['id'], 60)
        assert_command(
            ['dcos', 'marathon', 'deployment', 'stop', result[0]['id']])
        list_deployments(0, 'zero-instance-app')


def test_list_empty_task():
    _list_tasks(0)


def test_list_empty_task_not_running_app():
    with _zero_instance_app():
        _list_tasks(0)


def test_list_tasks():
    with _zero_instance_app():
        start_app('zero-instance-app', 3)
        watch_all_deployments()
        _list_tasks(3)


def test_list_tasks_table():
    with _zero_instance_app():
        start_app('zero-instance-app', 3)
        watch_all_deployments()
        assert_lines(['dcos', 'marathon', 'task', 'list'], 4)


def test_list_app_tasks():
    with _zero_instance_app():
        start_app('zero-instance-app', 3)
        watch_all_deployments()
        _list_tasks(3, 'zero-instance-app')


def test_list_missing_app_tasks():
    with _zero_instance_app():
        start_app('zero-instance-app', 3)
        watch_all_deployments()
        _list_tasks(0, 'missing-id')


def test_show_missing_task():
    returncode, stdout, stderr = exec_command(
        ['dcos', 'marathon', 'task', 'show', 'missing-id'])

    stderr = stderr.decode('utf-8')

    assert returncode == 1
    assert stdout == b''
    assert stderr.startswith("Task '")
    assert stderr.endswith("' does not exist\n")


def test_show_task():
    with _zero_instance_app():
        start_app('zero-instance-app', 3)
        watch_all_deployments()
        result = _list_tasks(3, 'zero-instance-app')

        returncode, stdout, stderr = exec_command(
            ['dcos', 'marathon', 'task', 'show', result[0]['id']])

        result = json.loads(stdout.decode('utf-8'))

        assert returncode == 0
        assert result['appId'] == '/zero-instance-app'
        assert stderr == b''


def test_stop_task():
    with _zero_instance_app():
        start_app('zero-instance-app', 1)
        watch_all_deployments()
        task_list = _list_tasks(1, 'zero-instance-app')
        task_id = task_list[0]['id']

        _stop_task(task_id)


def test_stop_task_wipe():
    with _zero_instance_app():
        start_app('zero-instance-app', 1)
        watch_all_deployments()
        task_list = _list_tasks(1, 'zero-instance-app')
        task_id = task_list[0]['id']

        _stop_task(task_id, '--wipe')


def test_kill_one_task():
    with _zero_instance_app():
        start_app('zero-instance-app', 1)
        watch_all_deployments()
        task_list = _list_tasks(1, 'zero-instance-app')
        task_id = [task_list[0]['id']]

        _kill_task(task_id)


def test_kill_two_tasks():
    with _zero_instance_app():
        start_app('zero-instance-app', 2)
        watch_all_deployments()
        task_list = _list_tasks(2, 'zero-instance-app')
        task_ids = [task['id'] for task in task_list]

        _kill_task(task_ids)


def test_kill_and_scale_task():
    with _zero_instance_app():
        start_app('zero-instance-app', 2)
        watch_all_deployments()
        task_list = _list_tasks(2, 'zero-instance-app')
        task_id = [task_list[0]['id']]

        _kill_task(task_id, scale=True)

        task_list = _list_tasks(1, 'zero-instance-app')


def test_kill_unknown_task():
    with _zero_instance_app():
        start_app('zero-instance-app')
        watch_all_deployments()
        task_id = ['unknown-task-id']

        _kill_task(task_id, expect_success=False)


def test_kill_task_wipe():
    with _zero_instance_app():
        start_app('zero-instance-app', 1)
        watch_all_deployments()
        task_list = _list_tasks(1, 'zero-instance-app')
        task_id = [task_list[0]['id']]

        _kill_task(task_id, wipe=True)


def test_stop_unknown_task():
    with _zero_instance_app():
        start_app('zero-instance-app')
        watch_all_deployments()
        task_id = 'unknown-task-id'

        _stop_task(task_id, expect_success=False)


def test_stop_unknown_task_wipe():
    with _zero_instance_app():
        start_app('zero-instance-app')
        watch_all_deployments()
        task_id = 'unknown-task-id'

        _stop_task(task_id, '--wipe', expect_success=False)


def test_bad_configuration(env):
    with update_config('marathon.url', 'http://localhost:88888', env):
        returncode, stdout, stderr = exec_command(
            ['dcos', 'marathon', 'about'], env=env)

        assert returncode == 1


def test_app_locked_error():
    with app('tests/data/marathon/apps/sleep_many_instances.json',
             '/sleep-many-instances',
             wait=False):
        stderr = b'Changes blocked: deployment already in progress for app.\n'
        assert_command(
            ['dcos', 'marathon', 'app', 'stop', 'sleep-many-instances'],
            returncode=1,
            stderr=stderr)


def test_ping():
    assert_command(['dcos', 'marathon', 'ping'],
                   stdout=b'Marathon ping response[1x]: "pong"\n')


def test_leader_show():
    returncode, stdout, stderr = exec_command(
        ['dcos', 'marathon', 'leader', 'show', '--json'])

    result = json.loads(stdout.decode('utf-8'))

    assert returncode == 0
    assert stderr == b''
    assert result['host'] == "marathon.mesos."
    assert 'ip' in result


def ignore_exception(exc):
    return isinstance(exc, Exception)


@pytest.fixture
def marathon_up():
    yield

    @retrying.retry(stop_max_delay=timedelta(minutes=5).total_seconds() * 1000,
                    retry_on_exception=ignore_exception)
    def check_marathon_up():
        # testing to see if marathon is up and can talk through the gateway
        # ignore the exception until we have a successful reponse.
        returncode, _, _ = exec_command(['dcos', 'marathon', 'app', 'list'])

        assert returncode == 0

    check_marathon_up()


@retrying.retry(stop_max_delay=timedelta(minutes=5).total_seconds() * 1000,
                retry_on_exception=ignore_exception)
def wait_marathon_down():
    returncode, _, _ = exec_command(['dcos', 'marathon', 'app', 'list'])

    assert returncode != 0


def test_leader_delete(marathon_up):
    assert_command(['dcos', 'marathon', 'leader', 'delete'],
                   stdout=b'Leadership abdicated\n')

    # There might be a slight delay until marathon shows itself as down,
    # so marathon_up() might succeed directly and the next tests would
    # run with an unhealthy marathon. Explicitly wait for marathon to
    # go down before waiting for it to become healthy again.
    wait_marathon_down()


@pytest.mark.skipif(sys.platform == 'win32',
                    reason="No pseudo terminal on windows")
def test_app_add_no_tty():
    proc, master = popen_tty('dcos marathon app add')

    stdout, stderr = proc.communicate()
    os.close(master)

    print(stdout)
    print(stderr)

    assert proc.wait() == 1
    assert stdout == b''
    assert stderr == (b"We currently don't support reading from the TTY. "
                      b"Please specify an application JSON.\n"
                      b"E.g.: dcos marathon app add < app_resource.json\n")


def _update_app(app_id, file_path):
    with open(file_path) as fd:
        returncode, stdout, stderr = exec_command(
            ['dcos', 'marathon', 'app', 'update', app_id],
            stdin=fd)

        assert returncode == 0
        assert stdout.decode().startswith('Created deployment ')
        assert stderr == b''


def _list_versions(app_id, expected_min_count, max_count=None):
    cmd = ['dcos', 'marathon', 'app', 'version', 'list', app_id]
    if max_count is not None:
        cmd.append('--max-count={}'.format(max_count))

    returncode, stdout, stderr = exec_command(cmd)

    result = json.loads(stdout.decode('utf-8'))

    assert returncode == 0
    assert isinstance(result, list)
    assert stderr == b''

    # Marathon persists app versions indefinitely by ID, so there may be extras
    assert len(result) >= expected_min_count

    if max_count is not None:
        assert len(result) <= max_count


def _list_tasks(expected_count=None, app_id=None):
    cmd = ['dcos', 'marathon', 'task', 'list', '--json']
    if app_id is not None:
        cmd.append(app_id)

    returncode, stdout, stderr = exec_command(cmd)

    result = json.loads(stdout.decode('utf-8'))

    assert returncode == 0
    if expected_count:
        assert len(result) == expected_count
    assert stderr == b''

    return result


def _stop_task(task_id, wipe=None, expect_success=True):
    cmd = ['dcos', 'marathon', 'task', 'stop', task_id]
    if wipe is not None:
        cmd.append('--wipe')

    returncode, stdout, stderr = exec_command(cmd)

    if expect_success:
        assert returncode == 0
        assert stderr == b''
        result = json.loads(stdout.decode('utf-8'))
        assert result['id'] == task_id
    else:
        assert returncode == 1


def _kill_task(task_ids, scale=None, wipe=None, expect_success=True):
    cmd = ['dcos', 'marathon', 'task', 'kill', '--json'] + task_ids
    if scale:
        cmd.append('--scale')
    if wipe:
        cmd.append('--wipe')
    returncode, stdout, stderr = exec_command(cmd)

    if expect_success:
        assert returncode == 0
        assert stderr == b''
        result = json.loads(stdout.decode('utf-8'))
        if scale:
            assert 'deploymentId' in result
        else:
            assert sorted(
                [task['id'] for task in result['tasks']]) == sorted(task_ids)

    else:
        assert returncode == 1


@contextlib.contextmanager
def _zero_instance_app():
    with app('tests/data/marathon/apps/zero_instance_sleep.json',
             'zero-instance-app'):
        yield


@contextlib.contextmanager
def _zero_instance_app_through_http():
    class JSONRequestHandler (BaseHTTPRequestHandler):

        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(open(
                'tests/data/marathon/apps/zero_instance_sleep.json',
                'rb').read())

    host = 'localhost'
    port = 12345
    server = HTTPServer((host, port), JSONRequestHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.setDaemon(True)
    thread.start()

    with app('http://{}:{}'.format(host, port), 'zero-instance-app'):
        try:
            yield
        finally:
            server.shutdown()
