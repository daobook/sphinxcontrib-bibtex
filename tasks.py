import inspect
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[0]
sys.path.extend([str(ROOT/'src')])
if not hasattr(inspect, 'getargspec'): # 修复
    inspect.getargspec = inspect.getfullargspec
    
from taolib.flows.tasks import sites

namespace = sites('doc', target='doc/_build/html')
