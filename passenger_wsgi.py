import sys
import os

# Add the application root to the path
sys.path.insert(0, os.path.dirname(__file__))

# Import the Flask application
from webhook import app as application
