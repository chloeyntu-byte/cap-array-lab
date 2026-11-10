; Load from the installed plugin copy. Each click captures its own window.
let((f)
  f = get_filename(piport)
  unless(substring(f 1 1) == "/" f = strcat(getWorkingDir() "/" f))
  caplabPluginLauncher = strcat(pcreReplace(pcreCompile("/[^/]*$") simplifyFilename(f) "" 1) "/launch.sh")
)

; POSIX shell quoting also preserves dollar signs and apostrophes in paths.
procedure(caplabMaestroShellQuote(value)
  let((quoted index character)
    quoted = "'"
    for(index 1 strlen(value)
      character = substring(value index 1)
      quoted = strcat(quoted if(character == "'" then "'\\''" else character))
    )
    strcat(quoted "'")
  )
)

unless(boundp('caplabMaestroLaunchJobs)
  caplabMaestroLaunchJobs = makeTable('caplabMaestroLaunchJobs nil)
)

; IPC pre-expands variables and quotes. Keep every dynamic argument inside an
; owned temporary script; only its generated ASCII path enters the IPC command.
procedure(caplabPrivateTemp(prefix)
  let((process output path)
    unless(member(prefix '("cal_ctx_" "cal_cmd_"))
      error("Cap Array Lab: unsupported private temporary-file prefix.\n")
    )
    ; IC618 makeTempFileName truncates long prefixes and returns a name only.
    ; mktemp creates the file atomically with 0600, independent of user umask.
    ; The command contains constants only; no user/source path reaches IPC.
    process = ipcBeginProcess(strcat("/usr/bin/mktemp /tmp/" prefix "XXXXXX"))
    unless(process error("Cap Array Lab: private-file creation could not start.\n"))
    output = ipcReadProcess(process 10)
    ipcWait(process)
    unless(equal(ipcGetExitStatus(process) 0) && stringp(output)
      error("Cap Array Lab: private-file creation failed; /usr/bin/mktemp is required.\n")
    )
    path = car(parseString(output "\r\n"))
    unless(path && rexMatchp(strcat("^/tmp/" prefix "[A-Za-z0-9]+$") path) && isFile(path)
      error("Cap Array Lab: private-file creation returned an invalid path.\n")
    )
    path
  )
)

procedure(caplabMaestroWriteLaunchScript(context)
  let((script port outcome)
    script = caplabPrivateTemp("cal_cmd_")
    unless(rexMatchp("^/tmp/cal_cmd_[A-Za-z0-9_]+$" script)
      when(isFile(script) deleteFile(script))
      error("Cap Array Lab: cannot create a safe local IPC transport path.\n")
    )
    ; mktemp created this file with private permissions.
    port = outfile(script)
    unless(port
      when(isFile(script) deleteFile(script))
      error("Cap Array Lab: cannot write the private IPC launch script.\n")
    )
    outcome = errset(
      progn(
        fprintf(port "#!/bin/bash\n/bin/rm -f -- \"$0\"\nexec %s --context %s\n"
          caplabMaestroShellQuote(caplabPluginLauncher) caplabMaestroShellQuote(context))
        t
      ) t)
    close(port)
    unless(outcome && car(outcome)
      when(isFile(script) deleteFile(script))
      error("Cap Array Lab: private IPC launch script export failed.\n")
    )
    script
  )
)

procedure(caplabMaestroLaunchOutput(_process data)
  when(stringp(data) && data != "" printf("%s" data))
)

procedure(caplabMaestroLaunchExit(process status)
  let((paths)
    paths = caplabMaestroLaunchJobs[process]
    foreach(path paths when(isFile(path) deleteFile(path)))
    remove(process caplabMaestroLaunchJobs)
  )
  unless(equal(status 0)
    printf("Cap Array Lab launch process exited with status %L; see CIW output.\n" status)
  )
)

procedure(caplabMaestroCurrentCdsLib()
  let((value result)
    value = nil
    ; The loaded mapping takes precedence over a later startup search.
    when(isCallable('ddGetUpdatedLib)
      result = errset(ddGetUpdatedLib() t)
      when(result && stringp(car(result)) && car(result) != ""
        value = car(result)
      )
    )
    unless(value
      when(isCallable('ddGetForcedLib)
        result = errset(ddGetForcedLib() t)
        when(result && stringp(car(result)) && car(result) != ""
          value = car(result)
        )
      )
    )
    unless(value
      when(isCallable('ddGetStartup)
        result = errset(ddGetStartup("cds.lib") t)
        when(result && stringp(car(result)) && car(result) != ""
          value = car(result)
        )
      )
    )
    value
  )
)


procedure(caplabWriteOneJobPolicy(port sess scope testName @optional identicalPolicy)
  let((result policy cursor key value)
    result = nil
    when(isCallable('maeGetJobPolicy)
      result = errset(if(testName then maeGetJobPolicy(?session sess ?testName testName)
                                 else maeGetJobPolicy(?session sess)) nil)
    )
    policy = if(result then car(result) else nil)
    fprintf(port "P\t%L\t%L\t%L\n" scope testName if(policy then t else nil))
    if(policy && identicalPolicy && equal(policy identicalPolicy) then
      ; This is equality of actual returned values, not an inheritance claim.
      fprintf(port "D\t%L\t%L\n" scope testName)
    else when(policy
      cursor = cdr(policy)
      while(cursor && cdr(cursor)
        key = car(cursor)
        value = cadr(cursor)
        when(symbolp(key)
          if(stringp(value) || numberp(value) || value == nil || value == t then
            fprintf(port "%s\t%L\t%L\t%L\t%L\n" if(numberp(value) then "N" else "J") scope testName symbolToString(key)
              if(numberp(value) then sprintf(nil "%L" value) else value))
          else
            fprintf(port "O\t%L\t%L\t%L\t%L\n" scope testName symbolToString(key)
              if(listp(value) then "list" else if(symbolp(value) then "symbol" else "native_object")))
          )
        )
        cursor = cddr(cursor)
      )
    ))
    policy
  )
)
procedure(caplabWriteJobPolicyRecords(port sess testNames)
  let((result globalPolicy value item)
    globalPolicy = caplabWriteOneJobPolicy(port sess "global" nil)
    foreach(name testNames caplabWriteOneJobPolicy(port sess "test" name globalPolicy))
    result = nil
    when(isCallable('maeGetCurrentRunMode)
      result = errset(maeGetCurrentRunMode(?session sess) nil)
    )
    fprintf(port "Q\t%L\n" if(result && stringp(car(result)) then car(result) else nil))
    result = nil
    when(isCallable('axlGetRunDistributeOptions)
      result = errset(axlGetRunDistributeOptions(axlGetActiveSetup(axlGetMainSetupDB(sess))) nil)
    )
    foreach(key '(RunIn DivideJobs)
      value = if(result && car(result) then get(car(result) key) else nil)
      when(symbolp(value) && value != nil && value != t value = symbolToString(value))
      fprintf(port "U\t%L\t%L\t%L\n" symbolToString(key)
        if(result && car(result) then t else nil) value)
    )
    when(result && car(result)
      foreach(item car(result)->JobLimit
        fprintf(port "U\t%L\tt\t%L\n" strcat("JobLimit:" car(item)) cadr(item))
      )
    )
  )
)
procedure(caplabWriteStorageReadback(port hsdb)
  let((result)
    result = nil
    when(isCallable('axlGetResultsLocation) result = errset(axlGetResultsLocation(hsdb) t))
    fprintf(port "V\t%L\t%L\t%L\n" "results_location" if(result && stringp(car(result)) then t else nil) if(result then car(result) else nil))
  )
)
procedure(caplabWriteSaveOptionsRecords(port hsdb)
  let((result)
    foreach(row '(("save_res_dir" "adexl.results" "saveResDir")
                  ("save_dir" "adexl.results" "saveDir")
                  ("project_dir" "asimenv.startup" "projectDir")
                  ("fallback" "adexl.results" "fallbackToDirWhenSimResultsUnspecified")
                  ("use_local_psf_dir" "adexl.results" "useLocalPsfDir")
                  ("use_local_rdb_dir" "adexl.results" "useLocalRdbDir")
                  ("local_psf_dir" "adexl.results" "saveLocalPsfDir")
                  ("local_rdb_dir" "adexl.results" "saveLocalRdbDir"))
      result = errset(envGetVal(cadr(row) caddr(row)) nil)
      fprintf(port "V\t%L\t%L\t%L\n" car(row) if(result then t else nil) if(result then car(result) else nil))
    )
    fprintf(port "V\t%L\tt\t%L\n" "source_home" getShellEnvVar("HOME"))
    fprintf(port "V\t%L\tt\t%L\n" "source_work_dir" getWorkingDir())
    caplabWriteStorageReadback(port hsdb)
  )
)
procedure(caplabWriteNativeHistoryPaths(path history)
  let((port name result rdb location)
    name = axlGetHistoryName(history)
    rdb = nil location = nil
    when(isCallable('axlGetHistoryResults)
      result = errset(axlGetHistoryResults(history) t)
      when(result && stringp(car(result)) rdb = car(result))
    )
    when(isCallable('axlGetHistoryResultsDir)
      result = errset(axlGetHistoryResultsDir(history) t)
      when(result && stringp(car(result)) location = car(result))
    )
    port = outfile(path)
    unless(port error("Cap Array Lab native history path readback cannot be created.\n"))
    fprintf(port "Cap Array Lab_NATIVE_HISTORY_STORAGE_1\nH\t%L\t%L\t%L\n" name rdb location)
    close(port)
  )
)


procedure(caplabMaestroWriteContext(session context @optional sourceWindow)
  let((setup lib cell view viewObj port outcome result history selected
       entry name variable libObj args handle windowNum modified policyTests)
    setup = axlGetMainSetupDB(session)
    unless(setup error("Cap Array Lab: this ADE window has no setup database.\n"))
    lib = axlGetSessionLibName(session)
    cell = axlGetSessionCellName(session)
    view = axlGetSessionViewName(session)
    viewObj = ddGetObj(lib cell view)
    unless(viewObj error("Cap Array Lab: cannot locate the invoking ADE view.\n"))
    selected = nil
    when(isCallable('axlGetCurrentHistory) && isCallable('axlGetHistoryName)
      result = errset(axlGetCurrentHistory(session) t)
      when(result
        history = car(result)
        when(history && history != 0
          result = errset(axlGetHistoryName(history) t)
          when(result && stringp(car(result)) selected = car(result))
        )
      )
    )
    windowNum = nil
    when(sourceWindow
      result = errset(sourceWindow->windowNum t)
      when(result && numberp(car(result)) windowNum = car(result))
    )
    ; A missing/failing API must not turn an unknown state into "saved".
    modified = "unknown"
    when(isCallable('maeIsSetupModified)
      result = errset(maeIsSetupModified(?session session) t)
      when(result && member(car(result) list(t nil)) modified = car(result))
    )
    port = outfile(context)
    unless(port error("Cap Array Lab: cannot write a private launch context.\n"))
    outcome = errset(
      progn(
        fprintf(port "CAPLAB_MAESTRO_CONTEXT_1\n")
        fprintf(port "C\t%L\t%L\t%L\t%L\t%L\t%L\t%L\n"
          lib cell view getWorkingDir() viewObj->readPath
          caplabMaestroCurrentCdsLib() prependInstallPath("bin/ocean"))
        fprintf(port "S\t%L\n" selected)
        fprintf(port "W\t%L\n" windowNum)
        fprintf(port "M\t%L\n" modified)
        fprintf(port "R\t%L\n" getShellEnvVar("LD_LIBRARY_PATH"))
        foreach(variable '("HOME" "PDK" "PATH" "CDS_LIC_FILE" "LM_LICENSE_FILE" "LD_PRELOAD"
                            "PYTHONHOME" "PYTHONPATH" "TCL_LIBRARY" "TK_LIBRARY" "QT_PLUGIN_PATH")
          fprintf(port "E\t%L\t%L\n" variable getShellEnvVar(variable))
        )
        when(isCallable('axlGetHistory) && isCallable('axlGetHistoryEntry)
             && isCallable('axlGetHistoryResultsDir) && isCallable('axlGetHistoryResults)
          result = errset(axlGetHistory(setup) t)
          when(result
            ; Native order is meaningful; do not sort by names or file mtime.
            foreach(name cadr(car(result))
              result = errset(axlGetHistoryEntry(setup name) t)
              when(result && car(result)
                entry = car(result)
                result = errset(list(axlGetHistoryResultsDir(entry)
                                    axlGetHistoryResults(entry)) t)
                when(result
                  fprintf(port "H\t%L\t%L\t%L\n" name
                    car(car(result)) cadr(car(result)))
                )
              )
            )
          )
        )
        when(isCallable('ddGetLibList)
          result = errset(ddGetLibList() t)
          when(result
            foreach(libObj car(result)
              result = errset(list(libObj->name libObj->readPath) t)
              when(result && stringp(car(car(result))) && stringp(cadr(car(result)))
                fprintf(port "L\t%L\t%L\n" car(car(result)) cadr(car(result)))
              )
            )
          )
        )
        when(isCallable('axlGetTests) && isCallable('axlGetTest)
             && isCallable('axlGetTestToolArgs)
          result = errset(axlGetTests(setup) t)
          when(result
            foreach(name cadr(car(result))
              result = errset(axlGetTest(setup name) t)
              when(result && car(result)
                handle = car(result)
                result = errset(axlGetTestToolArgs(handle) t)
                when(result
                  args = car(result)
                  fprintf(port "T\t%L\t%L\t%L\t%L\t%L\n" name
                    cadr(assoc("lib" args)) cadr(assoc("cell" args))
                    cadr(assoc("view" args)) cadr(assoc("sim" args)))
                )
              )
            )
          )
        )
        policyTests = nil
        when(isCallable('axlGetTests)
          result = errset(axlGetTests(setup) nil)
          when(result policyTests = cadr(car(result)))
        )
        caplabWriteJobPolicyRecords(port session policyTests)
        caplabWriteSaveOptionsRecords(port setup)
        t
      ) t)
    close(port)
    unless(outcome && car(outcome)
      error("Cap Array Lab: source context export failed; see CIW diagnostics.\n")
    )
    context
  )
)

procedure(caplabMaestroOpenCurrent()
  let((sourceWindow session context launchScript outcome process)
    sourceWindow = hiGetCurrentWindow()
    session = axlGetWindowSession(sourceWindow)
    unless(session error("Open Cap Array Lab from the source ADE window.\n"))
    unless(isFile(caplabPluginLauncher)
      error("Cap Array Lab: launcher is unavailable: %s\n" caplabPluginLauncher)
    )
    ; Allocate a fresh private context for this click.
    context = caplabPrivateTemp("cal_ctx_")
    outcome = errset(
      progn(
        caplabMaestroWriteContext(session context sourceWindow)
        ; Each click creates an independent UI bound to this window.
        launchScript = caplabMaestroWriteLaunchScript(context)
        process = ipcBeginProcess(strcat("/bin/bash " launchScript)
          "" 'caplabMaestroLaunchOutput 'caplabMaestroLaunchOutput 'caplabMaestroLaunchExit)
        unless(process error("Cap Array Lab: cannot start the workbench UI.\n"))
        caplabMaestroLaunchJobs[process] = list(launchScript context)
        t
      ) t)
    unless(outcome && car(outcome)
      when(launchScript && isFile(launchScript) deleteFile(launchScript))
      when(isFile(context) deleteFile(context))
      error("Cap Array Lab: launch failed; see CIW diagnostics.\n")
    )
    printf("Cap Array Lab launch requested for %s/%s/%s\n"
      axlGetSessionLibName(session) axlGetSessionCellName(session)
      axlGetSessionViewName(session))
    process
  )
)

procedure(caplabMaestroMenuItemFactory()
  hiCreateMenuItem(
    ?name 'caplabMaestroOpenItem ?itemText "Open Cap Array Lab"
    ?callback "caplabMaestroOpenCurrent()"
  )
)

procedure(caplabMaestroMenuTrigger(_args)
  list(hiCreatePulldownMenu('caplabMaestroMenu "Cap Array Lab"
    list(caplabMaestroMenuItemFactory())))
)
unless(boundp('caplabMaestroMenusRegistered) && caplabMaestroMenusRegistered
  deRegUserTriggers("explorer" nil 'caplabMaestroMenuTrigger)
  deRegUserTriggers("maestro" nil 'caplabMaestroMenuTrigger)
  caplabMaestroMenusRegistered = t
)
