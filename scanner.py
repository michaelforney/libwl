from __future__ import print_function
import getopt
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

pkg = "wl"

def interface_name(name):
    if not name:
        return None
    prefix = "{}_".format(pkg)
    if name.startswith(prefix):
        name = name[len(prefix):]
    elif name.startswith("wl_"):
        name = name.replace("_", ".", 1)
    return name.replace("_", "")

class Arg(object):
    def __init__(self, e):
        interface = interface_name(e.attrib.get("interface"))
        objtype = "{}#".format(interface or "wl.object")
        typemap = {
            "int": "int32",
            "uint": "uint32",
            "string": "byte[:]",
            "fd": "std.fd",
            "fixed": "wl.fixed",
            "array": "byte[:]",
            "object": "{}#".format(interface or "wl.object"),
            "new_id": "{}#".format(interface or "@a"),
        }
        unionmap = {
            "string": "data",
            "fd": "fd",
            "array": "data",
        }
        type = e.attrib["type"]
        myrtype = typemap[type]
        allownull = e.attrib.get("allow-null") == "true"
        if allownull:
            myrtype = "std.option({})".format(myrtype)

        self.name = e.attrib["name"].replace("_", "")
        self.type = type
        self.myrtype = myrtype
        self.union = unionmap.get(type, "int")
        self.interface = interface
        self.allownull = e.attrib.get("allow-null") == "true"

class Request(object):
    def __init__(self, e):
        self.name = e.attrib["name"].replace("_", "")
        self.args = [Arg(sub) for sub in e if sub.tag == "arg"]
        self.newid = None
        for arg in self.args:
            if arg.type == "new_id":
                self.newid = arg

    def isgeneric(self):
        return self.newid and not self.newid.interface

class Event(object):
    def __init__(self, e):
        name = e.attrib["name"].replace("_", "")
        args = []
        for sub in e:
            if sub.tag == "arg":
                args.append(Arg(sub))

        self.name = name
        self.args = args

class Entry(object):
    def __init__(self, e):
        self.name = e.attrib["name"].replace("_", "")
        self.value = e.attrib["value"]

class Enum(object):
    def __init__(self, e):
        self.name = e.attrib["name"].replace("_", "")
        self.entries = [Entry(sub) for sub in e if sub.tag == "entry"]

class Interface(object):
    def __init__(self, e):
        requests = []
        events = []
        enums = []
        for sub in e:
            if sub.tag == "request":
                requests.append(Request(sub))
            elif sub.tag == "event":
                events.append(Event(sub))
            elif sub.tag == "enum":
                enums.append(Enum(sub))

        self.name = interface_name(e.attrib["name"])
        self.requests = requests
        self.events = events
        self.enums = enums

def myrbool(x):
    return "true" if x else "false"

def generateclient(interfaces, f):
    f.write("use std\n")
    if pkg == "wl":
        f.write("use \"types\"\n")
        f.write("use \"util\"\n")
        f.write("use \"connection\"\n")
    else:
        f.write("use wl\n")
    f.write("\n")
    f.write("pkg {} =\n".format(pkg))
    first = True
    for i in interfaces:
        if first:
            first = False
        else:
            f.write("\n")
        f.write("\t/* {} */\n".format(i.name))
        if (pkg, i.name) != ("wl", "display"):
            f.write("\ttype {} = wl.object\n".format(i.name))
        for r in i.requests:
            decltype = "generic" if r.isgeneric() else "const"
            f.write("\t{} {}_{}: (obj: {}#".format(decltype, i.name, r.name, i.name))
            for arg in r.args:
                if arg == r.newid:
                    if not arg.interface:
                        f.write(", interface: byte[:], version: uint32")
                else:
                    f.write(", {}: {}".format(arg.name, arg.myrtype))
            f.write(" -> {})\n".format(r.newid.myrtype if r.newid else "void"))
        for e in i.events:
            f.write("\ttype {}_{} = struct\n".format(i.name, e.name))
            for arg in e.args:
                f.write("\t\t{}: {}\n".format(arg.name, arg.myrtype))
            f.write("\t;;\n")
        for e in i.enums:
            for entry in e.entries:
                f.write("\tconst {}{}_{}: uint32 = {}\n".format(
                    i.name.capitalize(), e.name, entry.name, entry.value))
        if i.events:
            f.write("\ttrait {}_listener @a =\n".format(i.name))
            for event in i.events:
                f.write("\t\t{}_{}: (l: @a#, ev: {}_{}# -> void)\n".format(
                    i.name, event.name, i.name, event.name))
            f.write("\t;;\n")
            f.write("\tgeneric {}_setlistener: (obj: {}#, l: @a::{}_listener# -> void)\n".format(
                i.name, i.name, i.name))
    f.write(";;\n")

    for i in interfaces:
        first = True
        obj = "dpy" if (pkg, i.name) == ("wl", "display") else "obj"
        for op, r in enumerate(i.requests):
            decltype = "generic" if r.isgeneric() else "const"
            argnames = "".join(", {}".format(arg.name) for arg in r.args if arg.type != "new_id")
            f.write("\n{} {}_{} = {{{}".format(decltype, i.name, r.name, obj))
            for arg in r.args:
                if arg == r.newid:
                    if not arg.interface:
                        f.write(", interface, version")
                else:
                    f.write(", {}".format(arg.name))
            f.write("\n")
            if obj == "dpy":
                f.write("\tvar obj = &dpy.obj\n")

            if r.newid:
                f.write("\tvar newobj = wl.mkobj(obj.conn)\n")
            f.write("\twl.marshal(obj.conn, (obj: wl.object#), {}, [\n".format(op))
            for arg in r.args:
                names = {
                    "new_id": "newobj.id",
                    "int": "({}: uint32)".format(arg.name),
                    "fixed": "({}: uint32)".format(arg.name),
                }
                name = names.get(arg.type, arg.name)
                if arg.allownull:
                    if arg.type == "object":
                        # Pretty ugly, but the best I could come up with.
                        name = "std.getv({}, (&wl.Nullobj: {}#)).id".format(name, arg.interface)
                    elif arg.type in ("string", "array"):
                        name = "({}, {})".format(name, myrbool(arg.type == "string"))
                else:
                    if arg.type == "object":
                        name += ".id"
                    elif arg.type in ("string", "array"):
                        name = "(`std.Some {}, {})".format(name, myrbool(arg.type == "string"))
                if arg == r.newid and not arg.interface:
                    f.write("\t\t`wl.Argdata (`std.Some interface, true),\n")
                    f.write("\t\t`wl.Argint version,\n")
                f.write("\t\t`wl.Arg{} {},\n".format(arg.union, name))
            f.write("\t][:])\n")
            if r.newid:
                f.write("\t-> (newobj: {})\n".format(r.newid.myrtype))
            f.write("}\n")

        if not i.events:
            continue
        f.write("\ngeneric {}_setlistener = {{{}, l\n".format(i.name, obj))
        if obj == "dpy":
            f.write("\tvar obj = &dpy.obj\n")
        f.write("\tobj.dispatch = `std.Some std.fndup({op, d\n")
        f.write("\t\tmatch op\n")
        for op, e in enumerate(i.events):
            f.write("\t\t| {}:\n".format(op))
            f.write("\t\t\tvar ev\n")
            for arg in e.args:
                if arg.type == "object":
                    if arg.interface:
                        o = "(o: {}#)".format(arg.interface)
                    else:
                        o = "o"
                    f.write("\t\t\tmatch mapget(&obj.conn.objs, wl.unmarshal(obj.conn, &d))\n")
                    if arg.allownull:
                        f.write("\t\t\t| `std.Some o: ev.{} = `std.Some {}\n".format(arg.name, o))
                        f.write("\t\t\t| `std.None: ev.{} = `std.None\n".format(arg.name))
                    else:
                        f.write("\t\t\t| `std.Some o: ev.{} = {}\n".format(arg.name, o))
                        # TODO: should log somewhere
                        f.write("\t\t\t| `std.None: -> void\n")
                    f.write("\t\t\t;;\n")
                elif arg.type in ("string", "array"):
                    if arg.allownull:
                        f.write("ev.{} = wl.unmarshaldata(&d)\n".format(arg.name))
                    else:
                        f.write("\t\t\tmatch wl.unmarshaldata(&d)\n")
                        f.write("\t\t\t| `std.Some s: ev.{} = s{}\n".format(
                            arg.name, "[:s.len-1]" if arg.type == "string" else ""))
                        # TODO: should log somewhere
                        f.write("\t\t\t| `std.None: -> void\n")
                        f.write("\t\t\t;;\n")
                else:
                    f.write("\t\t\tev.{} = wl.unmarshal(obj.conn, &d)\n".format(arg.name))
            f.write("\t\t\t{}_{}(l, &ev)\n".format(i.name, e.name))
        f.write("\t\t| _: std.fatal(\"unrecognized op\\n\")\n")
        f.write("\t\t;;\n")
        f.write("\t})\n")
        f.write("}\n")

def usage():
    print("usage: scanner.py [-d pkgconfigname] [-p pkg] protocol output", file=sys.stderr)
    sys.exit(2)

try:
    opts, args = getopt.getopt(sys.argv[1:], "d:p:")
except getopt.GetoptError:
    usage()
if len(args) != 2:
    usage()

infile, outfile = args
for opt, arg in opts:
    if opt == "-p":
        pkg = arg
    elif opt == "-d":
        pkgconf = os.getenv("PKG_CONFIG", "pkg-config")
        cmd = [pkgconf, "--variable", "pkgdatadir", arg]
        pkgdir = subprocess.check_output(cmd).decode().strip()
        infile = os.path.join(pkgdir, infile)

tree = ET.parse(infile)
root = tree.getroot()
interfaces = [Interface(e) for e in root if e.tag == "interface"]

try:
    with open(outfile, "w") as f:
        copyright = root.findtext("copyright")
        if copyright:
            f.write("/*\n")
            for line in copyright.strip().split("\n"):
                f.write(line.strip())
                f.write("\n")
            f.write("*/\n\n")
        generateclient(interfaces, f)
except:
    os.unlink(outfile)
    raise
