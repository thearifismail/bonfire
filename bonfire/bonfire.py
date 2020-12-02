#!/usr/bin/env python3

import click
import json
import logging
import sys

from tabulate import tabulate

import bonfire.config as conf
from bonfire.qontract import get_app_config
from bonfire.openshift import apply_config, oc_login, wait_for_all_resources
from bonfire.utils import split_equals
from bonfire.namespaces import (
    Namespace,
    get_namespaces,
    reserve_namespace,
    release_namespace,
    reset_namespace,
    add_base_resources,
    reconcile,
)

log = logging.getLogger(__name__)


def _error(msg):
    click.echo(f"ERROR: {msg}", err=True)
    sys.exit(1)


@click.group(context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--debug", "-d", help="Enable debug logging", is_flag=True, default=False)
def main(debug):
    logging.getLogger("sh").setLevel(logging.CRITICAL)  # silence the 'sh' library logger
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO)


@main.group()
def namespace():
    """perform operations on OpenShift namespaces"""
    pass


@main.group()
def config():
    """perform operations related to app configurations"""
    pass


def _reserve_namespace(duration, retries, namespace=None):
    if namespace:
        _warn_if_not_owner(namespace)
    ns = reserve_namespace(duration, retries, namespace)
    if not ns:
        _error("unable to reserve namespace")
    return ns.name


def _wait_on_namespace_resources(namespace, timeout):
    time_taken = wait_for_all_resources(namespace, timeout)
    if time_taken >= timeout:
        _error("Timed out waiting for resources; exiting")


def _prepare_namespace(namespace):
    add_base_resources(namespace)


def _warn_if_not_owner(namespace):
    ns = Namespace(name=namespace)
    if not ns.owned_by_me:
        if not click.confirm(
            f"Namespace currently reserved by someone else ({ns.requester_name}).  Continue anyway?"
        ):
            click.echo("Aborting")
            sys.exit(0)


_ns_reserve_options = [
    click.option(
        "--duration",
        "-d",
        required=False,
        type=int,
        default=60,
        help="duration of reservation in minutes (default: 60)",
    ),
    click.option(
        "--retries",
        "-r",
        required=False,
        type=int,
        default=0,
        help="how many times to retry namespace reserve before giving up (default: infinite)",
    ),
]

_ns_wait_options = [
    click.option(
        "--timeout",
        "-t",
        required=True,
        type=int,
        default=300,
        help="timeout in sec (default = 300) to wait for resources to be ready",
    )
]

_config_get_options = [
    click.option("--app", "-a", required=True, type=str, help="name of application"),
    click.option(
        "--src-env",
        "-e",
        help=f"Name of environment to pull app config from (default: {conf.EPHEMERAL_ENV_NAME})",
        type=str,
        default=conf.EPHEMERAL_ENV_NAME,
    ),
    click.option(
        "--ref-env",
        "-r",
        help=f"Name of environment to use for 'ref'/'IMAGE_TAG' (default: {conf.PROD_ENV_NAME})",
        type=str,
        default=conf.PROD_ENV_NAME,
    ),
    click.option(
        "--set-template-ref",
        "-t",
        help="Override template ref for a component using format '<component name>=<ref>'",
        multiple=True,
    ),
    click.option(
        "--set-image-tag",
        "-i",
        help="Override image tag for an image using format '<image name>=<tag>'",
        multiple=True,
    ),
    click.option(
        "--get-dependencies",
        "-d",
        help="Get config for any listed 'dependencies' in this app's ClowdApps",
        is_flag=True,
        default=False,
    ),
]


def common_options(options_list):
    """Click decorator used for common options if shared by multiple commands."""

    def inner(func):
        for option in reversed(options_list):
            func = option(func)
        return func

    return inner


@namespace.command("list")
@click.option(
    "--available", "-a", is_flag=True, default=False, help="show only un-reserved/ready namespaces"
)
def _list_namespaces(available):
    """Get list of ephemeral namespaces"""
    namespaces = get_namespaces(available_only=available)
    if not namespaces:
        click.echo("no namespaces found")
    else:
        data = {
            "NAME": [ns.name for ns in namespaces],
            "RESERVED": [str(ns.reserved).lower() for ns in namespaces],
            "READY": [str(ns.ready).lower() for ns in namespaces],
            "REQUESTER": [ns.requester_name for ns in namespaces],
            "EXPIRES IN": [ns.expires_in for ns in namespaces],
        }
        tabulated = tabulate(data, headers="keys")
        click.echo(tabulated)


@namespace.command("reserve")
@common_options(_ns_reserve_options)
@click.argument("namespace", required=False, type=str)
def _cmd_namespace_reserve(duration, retries, namespace):
    """Reserve an ephemeral namespace (specific or random)"""
    click.echo(_reserve_namespace(duration, retries, namespace))


@namespace.command("release")
@click.argument("namespace", required=True, type=str)
def _cmd_namespace_release(namespace):
    """Remove reservation from an ephemeral namespace"""
    _warn_if_not_owner(namespace)
    release_namespace(namespace)


@namespace.command("wait-on-resources")
@click.argument("namespace", required=True, type=str)
@common_options(_ns_wait_options)
def _cmd_namespace_wait_on_resources(namespace, timeout):
    """Wait for rolled out resources to be ready in namespace"""
    _wait_on_namespace_resources(namespace, timeout)


@namespace.command("prepare", hidden=True)
@click.argument("namespace", required=True, type=str)
def _cmd_namespace_prepare(namespace):
    """Copy base resources into specified namespace (for admin use only)"""
    _prepare_namespace(namespace)


@namespace.command("reconcile", hidden=True)
def _cmd_namespace_reconcile():
    """Run reconciler for namespace reservations (for admin use only)"""
    reconcile()


@namespace.command("reset", hidden=True)
@click.argument("namespace", required=True, type=str)
def _cmd_namespace_reset(namespace):
    """Set namespace to not released/not ready (for admin use only)"""
    reset_namespace(namespace)


def _get_app_config(
    app, src_env, ref_env, set_template_ref, set_image_tag, get_dependencies, namespace
):
    try:
        template_ref_overrides = split_equals(set_template_ref)
        image_tag_overrides = split_equals(set_image_tag)
    except ValueError as err:
        _error(str(err))
    app_config = get_app_config(
        app,
        src_env,
        ref_env,
        template_ref_overrides,
        image_tag_overrides,
        get_dependencies,
        namespace,
    )
    return app_config


@config.command("get")
@common_options(_config_get_options)
@click.option("--namespace", "-n", help="Namespace you intend to deploy these components into")
def _cmd_config_get(
    app, src_env, ref_env, set_template_ref, set_image_tag, get_dependencies, namespace
):
    """Get kubernetes config for an app and print the JSON"""
    print(
        json.dumps(
            _get_app_config(
                app, src_env, ref_env, set_template_ref, set_image_tag, get_dependencies, namespace
            ),
            indent=2,
        )
    )


@config.command("deploy")
@common_options(_config_get_options)
@click.option(
    "--namespace",
    "-n",
    help="Namespace to deploy to (default: none, bonfire will try to reserve one)",
    default=None,
)
@common_options(_ns_reserve_options)
@common_options(_ns_wait_options)
def _cmd_config_deploy(
    app,
    src_env,
    ref_env,
    set_template_ref,
    set_image_tag,
    get_dependencies,
    namespace,
    duration,
    retries,
    timeout,
):
    """Reserve a namespace, get app configs, and deploy to OpenShift"""
    requested_ns = namespace

    log.info("logging into OpenShift...")
    oc_login()
    log.info("reserving ephemeral namespace%s...", f" '{requested_ns}'" if requested_ns else "")
    ns = _reserve_namespace(duration, retries, requested_ns)

    try:
        log.info("getting app configs from qontract-server...")
        config = _get_app_config(
            app, src_env, ref_env, set_template_ref, set_image_tag, get_dependencies, ns
        )
        log.debug("app configs:\n%s", json.dumps(config, indent=2))
        log.info("applying app configs...")
        apply_config(ns, config)
        log.info("waiting on resources...")
        _wait_on_namespace_resources(ns, timeout)
    except (Exception, KeyboardInterrupt):
        log.exception("hit unexpected error!")
        try:
            if not requested_ns:
                log.info("releasing namespace '%s'", ns)
                release_namespace(ns)
        finally:
            _error("deploy failed")
    else:
        log.info("successfully deployed to %s", ns)
        print(ns)


if __name__ == "__main__":
    main()
