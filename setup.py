from setuptools import setup

setup(name='python-fastdotcom',
      version='0.0.1',
      description='Python API for testing internet speed on Fast.com',
      url='https://github.com/nkgilley/fast.com',
      author='Nolan Gilley',
      license='MIT',
      install_requires=['requests>=2.0'],
      packages=['fastdotcom'],
      zip_safe=True)
