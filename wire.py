"""Streaming XML for the disk-backed cache playlist."""
from html import escape


def xml_parts(name, value, root=False):
    if isinstance(value, list):
        for item in value: yield from xml_parts(name,item)
    elif isinstance(value, dict):
        attributes = ' xmlns="http://subsonic.org/restapi"' if root else ''
        for key, item in value.items():
            if not isinstance(item,(dict,list)) and item is not None and key!='value':
                text = str(item).lower() if isinstance(item,bool) else str(item)
                attributes += ' '+key+'="'+escape(text,quote=True)+'"'
        yield '<'+name+attributes+'>'
        if value.get('value') is not None: yield escape(str(value['value']))
        for key,item in value.items():
            if isinstance(item,(dict,list)): yield from xml_parts(key,item)
        yield '</'+name+'>'
    elif value is not None:
        yield '<'+name+'>'+escape(str(value))+'</'+name+'>'
