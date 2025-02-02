# -*- coding: utf-8 -*-
#
#  utils.py
#  This file is part of FORD.
#
#  Copyright 2015 Christopher MacMackin <cmacmackin@gmail.com>
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.
#
#

from __future__ import annotations

import re
import os.path
import json
from ford.sourceform import (
    FortranBase,
    FortranType,
    ExternalModule,
    ExternalFunction,
    ExternalSubroutine,
    ExternalInterface,
    ExternalType,
    ExternalVariable,
    ExternalBoundProcedure,
)

from urllib.error import URLError
from urllib.request import urlopen
from urllib.parse import urljoin
import pathlib
from typing import Dict, Union, TYPE_CHECKING
from io import StringIO
import itertools

if TYPE_CHECKING:
    from ford.fortran_project import Project

LINK_RE = re.compile(r"\[\[(\w+(?:\.\w+)?)(?:\((\w+)\))?(?::(\w+)(?:\((\w+)\))?)?\]\]")


# Dictionary for all macro definitions to be used in the documentation.
# Each key of the form |name| will be replaced by the value found in the
# dictionary in sub_macros.
_MACRO_DICT: Dict[str, str] = {}


def get_parens(line: str, retlevel: int = 0, retblevel: int = 0) -> str:
    """
    By default takes a string starting with an open parenthesis and returns the portion
    of the string going to the corresponding close parenthesis. If retlevel != 0 then
    will return when that level (for parentheses) is reached. Same for retblevel.
    """
    if not line:
        return line
    parenstr = ""
    level = 0
    blevel = 0
    for char in line:
        if char == "(":
            level += 1
        elif char == ")":
            level -= 1
        elif char == "[":
            blevel += 1
        elif char == "]":
            blevel -= 1
        elif (
            (char.isalpha() or char in ("_", ":", ",", " "))
            and level == retlevel
            and blevel == retblevel
        ):
            return parenstr
        parenstr = parenstr + char

    if level == retlevel and blevel == retblevel:
        return parenstr
    raise RuntimeError(f"Couldn't parse parentheses: {line}")


def strip_paren(line: str, retlevel: int = 0) -> list:
    """
    Takes a string with parentheses and removes any of the contents inside or outside
    of the retlevel of parentheses. Additionally, whenever a scope of the retlevel is
    left, the string is split.

    e.g. strip_paren("foo(bar(quz) + faz) + baz(buz(cas))", 1) -> ["(bar() + faz)", "(buz())"]
    """
    retstrs = []
    curstr = StringIO()
    level = 0
    for char in line:
        if char == "(":
            if level == retlevel or level + 1 == retlevel:
                curstr.write(char)
            level += 1
        elif char == ")":
            if level == retlevel or level - 1 == retlevel:
                curstr.write(char)
            if level == retlevel:
                # We are leaving a scope of the desired level,
                # and should split to indicate as such.
                retstrs.append(curstr.getvalue())
                curstr = StringIO()
            level -= 1
        elif level == retlevel:
            curstr.write(char)

    if curstr.getvalue() != "":
        retstrs.append(curstr.getvalue())
    return retstrs


def paren_split(sep, string):
    """
    Splits the string into pieces divided by sep, when sep is outside of parentheses.
    """
    if len(sep) != 1:
        raise ValueError("Separation string must be one character long")
    retlist = []
    level = 0
    blevel = 0
    left = 0
    for i in range(len(string)):
        if string[i] == "(":
            level += 1
        elif string[i] == ")":
            level -= 1
        elif string[i] == "[":
            blevel += 1
        elif string[i] == "]":
            blevel -= 1
        elif string[i] == sep and level == 0 and blevel == 0:
            retlist.append(string[left:i])
            left = i + 1
    retlist.append(string[left:])
    return retlist


def quote_split(sep, string):
    """
    Splits the strings into pieces divided by sep, when sep in not inside quotes.
    """
    if len(sep) != 1:
        raise ValueError("Separation string must be one character long")
    retlist = []
    squote = False
    dquote = False
    left = 0
    i = 0
    while i < len(string):
        if string[i] == '"' and not dquote:
            if not squote:
                squote = True
            elif (i + 1) < len(string) and string[i + 1] == '"':
                i += 1
            else:
                squote = False
        elif string[i] == "'" and not squote:
            if not dquote:
                dquote = True
            elif (i + 1) < len(string) and string[i + 1] == "'":
                i += 1
            else:
                dquote = False
        elif string[i] == sep and not dquote and not squote:
            retlist.append(string[left:i])
            left = i + 1
        i += 1
    retlist.append(string[left:])
    return retlist


def sub_links(string: str, project: Project) -> str:
    """
    Replace links to different parts of the program, formatted as
    [[name]] or [[name(object-type)]] with the appropriate URL. Can also
    link to an item's entry in another's page with the syntax
    [[parent-name:name]]. The object type can be placed in parentheses
    for either or both of these parts.
    """
    LINK_TYPES = {
        "module": "modules",
        "extmodule": "extModules",
        "type": "types",
        "exttype": "extTypes",
        "procedure": "procedures",
        "extprocedure": "extProcedures",
        "subroutine": "procedures",
        "extsubroutine": "extProcedures",
        "function": "procedures",
        "extfunction": "extProcedures",
        "proc": "procedures",
        "extproc": "extProcedures",
        "file": "allfiles",
        "interface": "absinterfaces",
        "extinterface": "extInterfaces",
        "absinterface": "absinterfaces",
        "extabsinterface": "extInterfaces",
        "program": "programs",
        "block": "blockdata",
    }

    SUBLINK_TYPES = {
        "variable": "variables",
        "type": "types",
        "constructor": "constructor",
        "interface": "interfaces",
        "absinterface": "absinterfaces",
        "subroutine": "subroutines",
        "function": "functions",
        "final": "finalprocs",
        "bound": "boundprocs",
        "modproc": "modprocs",
        "common": "common",
    }

    def convert_link(match):
        ERR = "Warning: Could not substitute link {}. {}"
        url = ""
        name = ""
        found = False
        searchlist = []
        item = None
        # [name,obj,subname,subobj]
        if not match.group(2):
            for key, val in LINK_TYPES.items():
                searchlist.extend(getattr(project, val))
        else:
            if match.group(2).lower() in LINK_TYPES:
                searchlist.extend(getattr(project, LINK_TYPES[match.group(2).lower()]))
            else:
                print(
                    ERR.format(
                        match.group(), f'Unrecognized classification "{match.group(2)}"'
                    )
                )
                return match.group()

        for obj in searchlist:
            if match.group(1).lower() == obj.name.lower():
                url = obj.get_url()
                name = obj.name
                found = True
                item = obj
                break
        else:
            print(ERR.format(match.group(), f'"{match.group(1)}" not found.'))
            url = ""
            name = match.group(1)

        if found and match.group(3):
            searchlist = []
            if not match.group(4):
                for key, val in SUBLINK_TYPES.items():
                    if val == "constructor":
                        if getattr(item, "constructor", False):
                            searchlist.append(item.constructor)
                        else:
                            continue
                    else:
                        searchlist.extend(getattr(item, val, []))
            else:
                if match.group(4).lower() in SUBLINK_TYPES:
                    if hasattr(item, SUBLINK_TYPES[match.group(4).lower()]):
                        if match.group(4).lower() == "constructor":
                            if item.constructor:
                                searchlist.append(item.constructor)
                        else:
                            searchlist.extend(
                                getattr(item, SUBLINK_TYPES[match.group(4).lower()])
                            )
                    else:
                        print(
                            ERR.format(
                                match.group(),
                                f'"{match.group(4)}" can not be contained in "{item.obj}"',
                            )
                        )
                        return match.group()
                else:
                    print(
                        ERR.format(
                            match.group(),
                            f'Unrecognized classification "{match.group(2)}".',
                        )
                    )
                    return match.group()

            for obj in searchlist:
                if match.group(3).lower() == obj.name.lower():
                    url = str(url) + "#" + obj.anchor
                    name = obj.name
                    item = obj
                    break
            else:
                print(
                    ERR.format(
                        match.group(),
                        f'"{match.group(3)}" not found in "{name}", linking to page for "{name}" instead.',
                    )
                )

        if found:
            return f'<a href="{url}">{name}</a>'
        return f"<a>{name}</a>"

    # Get information from links (need to build an RE)
    string = LINK_RE.sub(convert_link, string)
    return string


def register_macro(string):
    """Register a new macro definition of the form ``key = value``.
    In the documentation ``|key|`` can then be used to represent value.
    If key is already defined in the list of macros an `RuntimeError`
    will be raised.

    The function returns a tuple of the form ``(value, key)``, where
    key is ``None`` if no key definition is found in the string.

    """

    if "=" not in string:
        raise RuntimeError(f"Error, no alias name provided for {string}")

    chunks = string.split("=", 1)
    key = f"|{chunks[0].strip()}|"
    val = chunks[1].strip()

    if key in _MACRO_DICT and val != _MACRO_DICT[key]:
        # The macro is already defined. Do not overwrite it!
        # Can be ignored if the definition is the same...
        raise RuntimeError(
            f'Could not register macro "{key}" as "{val}"'
            f'because it is already defined as "{_MACRO_DICT[key]}"'
        )

    # Everything OK, add the macro definition to the dict.
    _MACRO_DICT[key] = val

    return (val, key)


def sub_macros(string):
    """
    Replaces macros in documentation with their appropriate values. These macros
    are used for things like providing URLs.
    """
    for key, val in _MACRO_DICT.items():
        string = string.replace(key, val)
    return string


def external(project, make=False, path="."):
    """
    Reads and writes the information needed for processing external modules.
    """

    # attributes of a module object needed for further processing
    ATTRIBUTES = [
        "pub_procs",
        "pub_absints",
        "pub_types",
        "pub_vars",
        "functions",
        "subroutines",
        "interfaces",
        "absinterfaces",
        "types",
        "variables",
        "boundprocs",
        "vartype",
        "permission",
        "generic",
    ]

    # Mapping between entity name and its type
    ENTITIES = {
        "module": ExternalModule,
        "interface": ExternalInterface,
        "type": ExternalType,
        "variable": ExternalVariable,
        "function": ExternalFunction,
        "subroutine": ExternalSubroutine,
        "boundprocedure": ExternalBoundProcedure,
    }

    def obj2dict(intObj):
        """
        Converts an object to a dictionary.
        """
        if hasattr(intObj, "external_url"):
            return None
        extDict = {
            "name": intObj.name,
            "external_url": intObj.get_url(),
            "obj": intObj.obj,
        }
        if hasattr(intObj, "proctype"):
            extDict["proctype"] = intObj.proctype
        if hasattr(intObj, "extends"):
            if isinstance(intObj.extends, FortranType):
                extDict["extends"] = obj2dict(intObj.extends)
            else:
                extDict["extends"] = intObj.extends
        for attrib in ATTRIBUTES:
            if not hasattr(intObj, attrib):
                continue

            attribute = getattr(intObj, attrib)

            if isinstance(attribute, list):
                extDict[attrib] = [obj2dict(item) for item in attribute]
            elif isinstance(attribute, dict):
                extDict[attrib] = {key: obj2dict(val) for key, val in attribute.items()}
            else:
                extDict[attrib] = str(attribute)
        return extDict

    def modules_from_local(url: pathlib.Path):
        """
        Get module information from an external project but on the
        local file system.
        """

        return json.loads((url / "modules.json").read_text(encoding="utf-8"))

    def dict2obj(extDict, url, parent=None, remote: bool = False) -> FortranBase:
        """
        Converts a dictionary to an object and immediately adds it to the project
        """
        name = extDict["name"]
        if extDict["external_url"]:
            extDict["external_url"] = extDict["external_url"].split("/", 1)[-1]
            if remote:
                external_url = urljoin(url, extDict["external_url"])
            else:
                external_url = url / extDict["external_url"]
        else:
            external_url = extDict["external_url"]

        # Look up what type of entity this is
        obj_type = extDict.get("proctype", extDict["obj"]).lower()
        # Construct the entity
        extObj = ENTITIES[obj_type](name, external_url, parent)
        # Now add it to the correct project list
        project_list = getattr(project, extObj._project_list)
        project_list.append(extObj)

        if obj_type == "interface":
            extObj.proctype = extDict["proctype"]
        elif obj_type == "type":
            extObj.extends = extDict["extends"]

        for key in ATTRIBUTES:
            if key not in extDict:
                continue
            if isinstance(extDict[key], list):
                tmpLs = [
                    dict2obj(item, url, extObj, remote) for item in extDict[key] if item
                ]
                setattr(extObj, key, tmpLs)
            elif isinstance(extDict[key], dict):
                tmpDict = {
                    key2: dict2obj(item, url, extObj, remote)
                    for key2, item in extDict[key].items()
                    if item
                }
                setattr(extObj, key, tmpDict)
            else:
                setattr(extObj, key, extDict[key])
        return extObj

    if make:
        # convert internal module object to a JSON database
        extModules = [obj2dict(module) for module in project.modules]
        (pathlib.Path(path) / "modules.json").write_text(json.dumps(extModules))
    else:
        # get the external modules from the external URLs
        for urldef in project.external:
            # get the external modules from the external URL
            url, short = register_macro(urldef)
            remote = re.match("https?://", url)
            try:
                if remote:
                    # Ensure the URL ends with '/' to have urljoin work as
                    # intentend.
                    if url[-1] != "/":
                        url = url + "/"
                    extModules = json.loads(
                        urlopen(urljoin(url, "modules.json")).read().decode("utf8")
                    )
                else:
                    url = pathlib.Path(url).resolve()
                    extModules = modules_from_local(url)
            except (URLError, json.JSONDecodeError) as error:
                extModules = []
                print(f"Could not open external URL '{url}', reason: {error}")
            # convert modules defined in the JSON database to module objects
            for extModule in extModules:
                dict2obj(extModule, url, remote=remote)


def str_to_bool(text):
    """Convert string to bool. Only takes 'true'/'false', ignoring case"""
    if isinstance(text, bool):
        return text
    if text.capitalize() == "True":
        return True
    if text.capitalize() == "False":
        return False
    raise ValueError(
        f"Could not convert string to bool: expected 'true'/'false', got '{text}'"
    )


def normalise_path(
    base_dir: pathlib.Path, path: Union[str, pathlib.Path]
) -> pathlib.Path:
    """Tidy up path, making it absolute, relative to base_dir"""
    return (base_dir / os.path.expandvars(path)).absolute()


def traverse(root, attrs) -> list:
    """Traverse a tree of objects, returning a list of all objects found
    within the attributes attrs"""
    nodes = []
    for obj in itertools.chain(*[getattr(root, attr, []) for attr in attrs]):
        nodes.append(obj)
        nodes.extend(traverse(obj, attrs))
    return nodes
