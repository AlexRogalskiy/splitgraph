"""
Public API for Splitgraph
"""
import logging

from ._data.images import get_all_image_info, get_full_object_tree
from ._data.registry import publish_tag, unpublish_repository, get_published_info
from .config import CONFIG
from .engine import get_engine, switch_engine
from .exceptions import SplitGraphException
from .splitfile import *

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
