import tempfile
import os
import yaml
import subprocess
import shutil

import http
from socket import *
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading


import socket
from contextlib import closing
import threading
import functools

from http.server import HTTPServer, BaseHTTPRequestHandler
import ssl


import os

THIS_DIR = os.path.dirname(os.path.realpath(__file__))
TESTPAGE_DIR = os.path.join(THIS_DIR, "testpage")


def find_free_port():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def serve_forever(httpd, work_dir, port):

    httpd.serve_forever()


def create_html(work_dir):

    shutil.copy(os.path.join(TESTPAGE_DIR, "empty.html"), work_dir)
    shutil.copy(os.path.join(TESTPAGE_DIR, "worker.js"), work_dir)


def get_pytest_files(recipe_dir, recipe):
    recipe_dir = os.path.abspath(recipe_dir)
    extra = recipe.get("extra", {})
    emscripten_tests = extra.get("emscripten_tests", {})
    python = emscripten_tests.get("python", {})
    pytest_files = python.get("pytest_files", [])
    pytest_files = [os.path.join(recipe_dir, f) for f in pytest_files]
    return pytest_files


def create_test_env(pkg_name, prefix):
    # cmd = ['$MAMBA_EXE' ,'create','--prefix', prefix,'--platform=emscripten-32'] + [pkg_name] #+ ['--dryrun']
    print("prefix", prefix)
    cmd = [
        f"""$MAMBA_EXE create --yes --prefix {prefix} --platform=emscripten-32   python "pytest_driver=0.5.1" pytest {pkg_name}"""
    ]
    ret = subprocess.run(cmd, shell=True)
    #  stderr=subprocess.PIPE, stdout=subprocess.PIPE)
    returncode = ret.returncode
    assert returncode == 0


def pack(prefix, pytest_files):
    print("pytest_files", pytest_files)
    assert len(pytest_files) <= 1, "atm only one file is allowed"

    cmd = [f"empack pack python core {prefix} --version=3.10 "]
    ret = subprocess.run(cmd, shell=True)

    cmd = [f"empack pack file  {pytest_files[0]}  '/tests'  testdata"]
    ret = subprocess.run(cmd, shell=True)


def copy_from_prefix(prefix, work_dir):
    # copy wasm / js file to work dir
    js_file = os.path.join(prefix, "bin", "pytest_driver.js")
    shutil.copy(js_file, work_dir)
    wasm_file = os.path.join(prefix, "bin", "pytest_driver.wasm")
    shutil.copy(wasm_file, work_dir)


def start_server(work_dir, port):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=work_dir, **kwargs)

        def log_message(self, format, *args):
            return

    httpd = HTTPServer(("localhost", port), Handler)

    thread = threading.Thread(
        target=functools.partial(
            serve_forever, httpd=httpd, work_dir=work_dir, port=port
        )
    )
    thread.start()
    return thread, httpd


CONF = """// @ts-check

/** @type {import('@playwright/test').PlaywrightTestConfig} */
const config = {
  use: {
    headless: false,
    viewport: { width: 1280, height: 720 },
    ignoreHTTPSErrors: true,
    video: 'on-first-retry',
  },
};

module.exports = config;
"""

import sys
import asyncio
from playwright.async_api import async_playwright
from playwright.async_api import Page


async def playwright_main(page_url):
    async with async_playwright() as p:
        # browser = await p.chromium.launch(headless=False, slow_mo=100)
        browser = await p.chromium.launch(headless=True)  # slow_mo=1000)
        page = await browser.new_page()

        async def handle_worker(worker):
            test_output = await worker.evaluate_handle(
                """async () => 
            {
                const sink = (text) =>{}
                var pytestOutputString = ""
                const print = (text) => {
                  console.log(text)
                  pytestOutputString += text;
                  pytestOutputString += "\\n";
                }

                function waitRunDependency() {
                  const promise = new Promise((r) => {
                    Module.monitorRunDependencies = (n) => {
                      if (n === 0) {
                        r();
                      }
                    };
                  });
                  Module.addRunDependency("dummy");
                  Module.removeRunDependency("dummy");
                  return promise;
                }


                var myModule = await createModule({print:print,error:print})
                var Module = myModule


                globalThis.Module = Module
                await import('./python_data.js')
                await import('./testdata.js')
                var deps = await waitRunDependency()
                myModule.initialize_interpreter()
                var ret = myModule.run_tests("/tests")
                var msg = {
                    return_code: ret,
                    pytest_output: pytestOutputString
                }
                self.postMessage(msg)
                return msg
            }"""
            )

        page.on("worker", handle_worker)
        await page.goto(page_url)
        await page.wait_for_function("() => globalThis.done")

        test_output = await page.evaluate_handle(
            """
            () => 
            {   
            return globalThis.test_output
            }
        """
        )
        return_code = int(str(await test_output.get_property("return_code")))
        pytest_output = await test_output.get_property("pytest_output")
        await browser.close()
        print(pytest_output)
        if return_code != 0:
            sys.exit(return_code)


def run_playwright_tests(work_dir, port):
    # Write the file out again
    page_url = f"http://0.0.0.0:{port}/empty.html"
    os.chdir(work_dir)

    asyncio.run(playwright_main(page_url=page_url))


def test_package(recipe):
    recipe_dir, _ = os.path.split(recipe["recipe_file"])
    assert os.path.isdir(recipe_dir), f"recipe_dir: {recipe_dir} does not exist"
    recipe_file = os.path.join(recipe_dir, "recipe.yaml")

    pytest_files = get_pytest_files(recipe_dir, recipe)
    has_tests = len(pytest_files) > 0

    old_cwd = os.getcwd()

    if has_tests:
        pkg_name = recipe["package"]["name"]
        with tempfile.TemporaryDirectory() as temp_dir:

            prefix = os.path.join(temp_dir, "prefix")
            work_dir = os.path.join(temp_dir, "work")
            os.mkdir(work_dir)
            os.chdir(work_dir)
            create_test_env(pkg_name=pkg_name, prefix=prefix)
            pack(prefix=prefix, pytest_files=pytest_files)
            port = find_free_port()
            create_html(work_dir=work_dir)

            thread, server = start_server(work_dir=work_dir, port=port)
            try:
                copy_from_prefix(prefix=prefix, work_dir=work_dir)
                run_playwright_tests(work_dir=work_dir, port=port)
            finally:
                server.shutdown()
                thread.join()
    os.chdir(old_cwd)
