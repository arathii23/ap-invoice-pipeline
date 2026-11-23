"""Test package marker: makes `python3 -m unittest tests.test_xx` work as well as `python3 -m unittest discover -s tests`."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
