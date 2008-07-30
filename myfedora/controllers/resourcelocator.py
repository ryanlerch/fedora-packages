from myfedora.lib.base import BaseController
from tg import expose
import pylons

class ResourcelocatorController(BaseController):
    def __init__(self):
        super(ResourcelocatorController, self).__init__()

    @expose()
    def lookup(self, *args):
        resource = args[0]
        r = pylons.app_globals.resourceviews.get(resource, None)
        
        if r:
            return r.controller, list(args)