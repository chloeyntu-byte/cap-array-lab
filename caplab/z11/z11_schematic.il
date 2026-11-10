; Inert Python plan -> native analogLib schematic.  Never evaluate plan data.
; Public entry: z11CreateSchematic(lib cell plan expectedLibPath), returns t.

procedure(z11SchPointCenter(box)
  list((xCoord(car(box))+xCoord(cadr(box)))/2.0
       (yCoord(car(box))+yCoord(cadr(box)))/2.0)
)

procedure(z11SchMasterPin(master name)
  let((term pin fig)
    term = car(setof(term master~>terminals term~>name == name))
    unless(term error("Z11: %s missing real master terminal %s\n" master~>cellName name))
    pin = car(term~>pins)
    fig = or(pin~>fig car(pin~>figs))
    unless(fig error("Z11: no graphical pin for %s/%s\n" master~>cellName name))
    z11SchPointCenter(fig~>bBox)
  )
)

procedure(z11SchSamePoint(a b)
  abs(xCoord(a)-xCoord(b)) < 0.0000001 && abs(yCoord(a)-yCoord(b)) < 0.0000001
)

procedure(z11SchWire(cv points net)
  let((wire position)
    unless(z11SchSamePoint(car(points) cadr(points))
      wire = schCreateWire(cv "draw" "full" points 0.0625 0.0625 0.0)
      unless(wire error("Z11: cannot create native wire on %s\n" net))
      position = z11SchPointCenter(list(car(points) cadr(points)))
      unless(schCreateWireLabel(cv nil position net "lowerCenter" "R0" "stick" 0.0625 nil)
        error("Z11: cannot create wire label %s\n" net)
      )
    )
  )
)

procedure(z11SchOrientation(master targets)
  let((a b ta tb vx vy dx dy orient rotated found)
    a = z11SchMasterPin(master car(car(targets)))
    b = z11SchMasterPin(master car(cadr(targets)))
    ta = cadr(car(targets)) tb = cadr(cadr(targets))
    dx = xCoord(tb)-xCoord(ta) dy = yCoord(tb)-yCoord(ta)
    foreach(candidate list("R0" "R90" "R180" "R270")
      rotated = dbTransformPoint(list(xCoord(b)-xCoord(a) yCoord(b)-yCoord(a)) list(0.0:0.0 candidate 1.0))
      vx = xCoord(rotated) vy = yCoord(rotated)
      when(abs(vx*dy-vy*dx) < 0.0000001 && vx*dx+vy*dy > 0.0
        orient = candidate found = t
      )
    )
    unless(found error("Z11: master pin axis cannot match editor geometry\n"))
    orient
  )
)

procedure(z11SchPropString(spec name default)
  let((value)
    value = assoc(name nth(4 spec))
    if(value then cadr(value) else default)
  )
)

procedure(z11SchDisplayNumber(value suffix)
  let((numeric)
    numeric = if(suffix then substring(value 1 strlen(value)-1) else value)
    sprintf(nil "%.6g" atof(numeric))
  )
)

procedure(z11SchCaption(cv point text role justification)
  let((label)
    ; Device/drawing is visible wherever the actual native device is visible.
    ; Default CDF annotation layers can be hidden in an existing user session.
    label = dbCreateLabel(cv list("device" "drawing") point text
                         justification "R0" "roman" 0.105)
    unless(label error("Z11: cannot create readable schematic caption\n"))
    dbReplaceProp(label "z11CaptionRole" "string" role)
    label
  )
)

procedure(z11SchInstanceCaptions(cv spec)
  let((centre cell source value q first second x y justification)
    centre = caddr(spec) cell = cadr(spec)
    source = z11SchPropString(spec "z11SourceId" car(spec))
    x = xCoord(centre) y = yCoord(centre)
    cond(
      ((cell == "mind")
        value = z11SchDisplayNumber(z11SchPropString(spec "k" "0") nil)
        first = strcat(source "  k=" value)
        second = strcat(z11SchPropString(spec "ind1" "") " <-> " z11SchPropString(spec "ind2" ""))
        x = x+0.32 y = y+0.075 justification = "lowerLeft"
      )
      (member(cell list("ind" "indq"))
        value = z11SchDisplayNumber(z11SchPropString(spec "l" "0p") t)
        first = strcat(source "  " value "pH")
        q = z11SchPropString(spec "q" "inf")
        second = strcat("Q=" if(q == "inf" then q else z11SchDisplayNumber(q nil)))
      )
      (member(cell list("cap" "capq"))
        value = z11SchDisplayNumber(z11SchPropString(spec "c" "0f") t)
        first = strcat(source "  " value "fF")
        q = z11SchPropString(spec "q" "inf")
        second = strcat("Q=" if(q == "inf" then q else z11SchDisplayNumber(q nil)))
      )
      (t
        value = z11SchDisplayNumber(z11SchPropString(spec "r" "0") nil)
        first = strcat(source "  " value "ohm")
      )
    )
    unless(cell == "mind"
      if(member(nth(3 spec) list(1 3)) then
        x = x+0.20 y = y+0.075 justification = "lowerLeft"
      else
        y = y+0.31 justification = "lowerCenter"
      )
    )
    z11SchCaption(cv list(x y) first "device_value" justification)
    when(second z11SchCaption(cv list(x y-0.145) second "device_detail" justification))
  )
)

procedure(z11SchCreateInstance(cv spec)
  let((master inst centre targets orient localCentre rotated origin p q dotPoint dotBox)
    master = dbOpenCellViewByType("analogLib" cadr(spec) "symbol" "" "r")
    unless(master error("Z11: missing analogLib/%s/symbol\n" cadr(spec)))
    centre = caddr(spec) targets = nth(5 spec)
    if(targets then
      orient = z11SchOrientation(master targets)
      localCentre = z11SchPointCenter(list(z11SchMasterPin(master car(car(targets)))
                                               z11SchMasterPin(master car(cadr(targets)))))
    else
      orient = nth(nth(3 spec) list("R0" "R270" "R180" "R90"))
      localCentre = z11SchPointCenter(master~>bBox)
    )
    rotated = dbTransformPoint(localCentre list(0.0:0.0 orient 1.0))
    origin = list(xCoord(centre)-xCoord(rotated) yCoord(centre)-yCoord(rotated))
    inst = schCreateInst(cv master car(spec) origin orient)
    unless(inst error("Z11: cannot instantiate %s\n" car(spec)))
    foreach(prop nth(4 spec)
      dbReplaceProp(inst car(prop) "string" cadr(prop))
    )
    foreach(target targets
      p = dbTransformPoint(z11SchMasterPin(master car(target)) inst~>transform)
      q = cadr(target)
      z11SchWire(cv list(p q) caddr(target))
    )
    when(member(cadr(spec) list("ind" "indq"))
      ; PLUS is the physical dotted endpoint; rotating the native instance
      ; handles the source dot polarity, while mind keeps positive k.
      dotPoint = dbTransformPoint(z11SchMasterPin(master "PLUS") inst~>transform)
      dotBox = list(list(xCoord(dotPoint)+0.04 yCoord(dotPoint)+0.04)
                    list(xCoord(dotPoint)+0.10 yCoord(dotPoint)+0.10))
      dbCreateEllipse(cv list("device" "drawing") dotBox)
    )
    z11SchInstanceCaptions(cv spec)
    dbClose(master)
    inst
  )
)

procedure(z11SchVerify(cv plan)
  let((inst term prop actual expected)
    unless(length(setof(i cv~>instances i~>libName == "analogLib")) == length(cadr(plan))
      error("Z11: native instance count changed after Check & Save\n"))
    foreach(spec cadr(plan)
      inst = car(setof(i cv~>instances i~>name == car(spec)))
      unless(inst && inst~>libName == "analogLib" && inst~>cellName == cadr(spec)
        error("Z11: wrong native master for %s\n" car(spec)))
      foreach(value nth(4 spec)
        prop = dbFindProp(inst car(value))
        unless(prop && prop~>value == cadr(value)
          error("Z11: actual property mismatch %s/%s\n" car(spec) car(value)))
      )
      foreach(target nth(5 spec)
        term = car(setof(it inst~>instTerms it~>name == car(target)))
        unless(term && term~>net && term~>net~>name == caddr(target)
          error("Z11: connectivity mismatch %s/%s: expected %s actual %L\n"
            car(spec) car(target) caddr(target) term~>net~>name))
      )
    )
    unless(length(cv~>terminals) == length(nth(3 plan)) error("Z11: external terminal count mismatch\n"))
    foreach(pin nth(3 plan)
      term = car(setof(term cv~>terminals term~>name == car(pin)))
      unless(term && term~>net && term~>net~>name == caddr(pin)
        error("Z11: external terminal mismatch %s\n" car(pin)))
    )
    t
  )
)

procedure(z11SchBuild(cv plan)
  let((pinMaster p q noteOrigin wireResult)
    foreach(spec cadr(plan) z11SchCreateInstance(cv spec))
    foreach(wire caddr(plan) z11SchWire(cv cadr(wire) car(wire)))
    pinMaster = dbOpenCellViewByType("basic" "iopin" "symbol" "" "r")
    unless(pinMaster error("Z11: missing basic/iopin/symbol\n"))
    foreach(pin nth(3 plan)
      p = cadr(pin)
      unless(schCreatePin(cv pinMaster car(pin) "inputOutput" nil p "R0")
        error("Z11: cannot create external terminal %s\n" car(pin)))
      ; Separate-net PORT endpoints are never connected through a source/load.
      q = list(xCoord(p)+0.0625 yCoord(p))
      z11SchWire(cv list(p q) caddr(pin))
      z11SchCaption(cv list(xCoord(p)+0.14 yCoord(p)+0.04) car(pin) "port" "lowerLeft")
    )
    dbClose(pinMaster)
    foreach(meta nth(4 plan) dbReplaceProp(cv strcat("z11_" car(meta)) "string" cadr(meta)))
    noteOrigin = list(xCoord(car(cv~>bBox)) yCoord(cadr(cv~>bBox))+0.375)
    z11SchCaption(cv noteOrigin
      strcat("Q: indq at fq=" cadr(assoc("q_reference_frequency" nth(4 plan))) "; capq parallel Q")
      "q_model_note" "lowerLeft")
    wireResult = schCheck(cv)
    unless(wireResult && (wireResult == t || (listp(wireResult) && car(wireResult) == 0))
      error("Z11: native schematic Check failed: %L\n" wireResult))
    z11SchVerify(cv plan)
    unless(dbSave(cv) error("Z11: native schematic save failed\n"))
    t
  )
)

procedure(z11CreateSchematic(lib cell plan expectedLibPath)
  let((libObj cv checkCv built result created target)
    unless(stringp(lib) && stringp(cell) && stringp(expectedLibPath)
      error("Z11: library identity arguments must be strings\n"))
    unless(rexMatchp("^[A-Za-z][A-Za-z0-9_]*$" cell)
      error("Z11: invalid target cell name\n"))
    unless(listp(plan) && length(plan) == 5 && car(plan) == 1
      error("Z11: unsupported schematic plan\n"))
    unless(length(cadr(plan)) <= 200 && length(caddr(plan)) <= 15000 && member(length(nth(3 plan)) list(1 2))
      error("Z11: invalid export plan size\n"))
    libObj = ddGetObj(lib)
    unless(libObj && libObj~>readPath == expectedLibPath
      error("Z11: active Maestro library path changed; refusing export\n"))
    when(member(lib list("analogLib" "basic"))
      error("Z11: target must be a user library\n"))
    when(ddGetObj(lib cell "schematic")
      error("Z11: existing %s/%s/schematic will not be overwritten\n" lib cell))
    cv = dbOpenCellViewByType(lib cell "schematic" "schematic" "w")
    unless(cv error("Z11: cannot create %s/%s/schematic\n" lib cell))
    created = t
    result = errset(z11SchBuild(cv plan) t)
    dbClose(cv) cv = nil
    when(result
      checkCv = dbOpenCellViewByType(lib cell "schematic" "" "r")
      result = errset(z11SchVerify(checkCv plan) t)
      when(checkCv dbClose(checkCv)) checkCv = nil
    )
    unless(result
      ; Only this newly created view is owned here; preserve every existing view.
      target = ddGetObj(lib cell "schematic")
      when(created && target ddDeleteObj(target))
      error("Z11: export verification failed for %s/%s; new schematic was removed\n" lib cell)
    )
    printf("Z11_SCHEMATIC_VERIFIED lib=%s cell=%s instances=%d\n" lib cell length(cadr(plan)))
    t
  )
)
