#!/usr/bin/env python3
import argparse
from caplab.model import ROOT
from caplab.ui import App

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description='Cap Array Lab')
    parser.add_argument('--project', help='Open a saved project')
    parser.add_argument('--context', help='Private invoking Maestro context')
    parser.add_argument('--tank', action='store_true', help='Open as Tank Scan Lab (N-port + cap array + gm startup analysis)')
    args=parser.parse_args()
    context = None
    if args.context:
        from caplab.launch_context import load_context
        context = load_context(args.context)
        import json, os
        os.environ["CAPLAB_NATIVE_ENV_JSON"] = json.dumps(context["native_environment"])
        os.environ["CAPLAB_NATIVE_VIRTUOSO"] = os.path.join(os.path.dirname(context["source"]["ocean"]), "virtuoso")
    previous=ROOT/'.work'/('last_session_tank.json' if args.tank else 'last_session.json')
    path=args.project or (str(previous) if not context and previous.is_file() else None)
    app=App(launch_context=context,tank=args.tank) if context else App(tank=args.tank)
    if path:app.after_idle(lambda:app.load_workbook_path(path,session=not bool(args.project)))
    app.mainloop()
