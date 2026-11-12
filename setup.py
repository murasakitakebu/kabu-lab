from setuptools import setup, find_packages

setup(
    name="kabu-lab",
    packages=find_packages(include=["core", "core.*"]),
)
