import os
import sys
import radical.pilot as rp

# READ: The RADICAL-Pilot documentation: 
#   http://radicalpilot.readthedocs.org/en/latest
#
# Try running this example with RADICAL_PILOT_VERBOSE=debug set if 
# you want to see what happens behind the scenes!


#------------------------------------------------------------------------------
#
def pilot_state_cb (pilot, state) :
    """ this callback is invoked on all pilot state changes """

    print "[Callback]: ComputePilot '%s' state: %s." % (pilot.uid, state)

    if  state == rp.FAILED :
        sys.exit (1)


#------------------------------------------------------------------------------
#
def unit_state_cb (unit, state) :
    """ this callback is invoked on all unit state changes """

    print "[Callback]: ComputeUnit '%s' state: %s." % (unit.uid, state)

    if  state == rp.FAILED :
        sys.exit (1)


#------------------------------------------------------------------------------
#
if __name__ == "__main__":

    # Create a new session. A session is the 'root' object for all other
    # RADICAL-Pilot objects. It encapsulates the MongoDB connection(s) as
    # well as security credentials.
    session = rp.Session()

    # Add a Pilot Manager. Pilot managers manage one or more ComputePilots.
    pmgr = rp.PilotManager(session=session)

    # Register our callback with the PilotManager. This callback will get
    # called every time any of the pilots managed by the PilotManager
    # change their state.
    pmgr.register_callback(pilot_state_cb)

    # Define a single-core local pilot that runs for 5 minutes and cleans up
    # after itself.
    pdesc = rp.ComputePilotDescription()
    pdesc.resource = "localhost"
    pdesc.cores    = 8
    pdesc.runtime  = 5 # Minutes
    #pdesc.cleanup  = True

    # Launch the pilot.
    pilot = pmgr.submit_pilots(pdesc)

    input_sd = {
        'source': '/etc/passwd',
        'target': 'input.dat'
    }

    output_sd = {
        'source': 'result.dat',
        'target': '/tmp/result.dat'
    }

    # Create a Compute Unit that sorts the local password file and writes the
    # output to result.dat.
    #
    #  The exact command that is executed by the agent is:
    #    "/usr/bin/sort -o result.dat input.dat"
    #
    cud = rp.ComputeUnitDescription()
    cud.executable     = "sort"
    cud.arguments      = ["-o", "result.dat", "input.dat"]
    cud.input_staging  = input_sd
    cud.output_staging = output_sd

    # Combine the ComputePilot, the ComputeUnits and a scheduler via
    # a UnitManager object.
    umgr = rp.UnitManager(session, rp.SCHED_DIRECT_SUBMISSION)

    # Register our callback with the UnitManager. This callback will get
    # called every time any of the units managed by the UnitManager
    # change their state.
    umgr.register_callback(unit_state_cb)

    # Add the previously created ComputePilot to the UnitManager.
    umgr.add_pilots(pilot)

    # Submit the previously created ComputeUnit description to the
    # PilotManager. This will trigger the selected scheduler to start
    # assigning the ComputeUnit to the ComputePilot.
    unit = umgr.submit_units(cud)

    # Wait for the compute unit to reach a terminal state (DONE or FAILED).
    umgr.wait_units()

    print "* Task %s (executed @ %s) state: %s, exit code: %s, started: %s, " \
          "finished: %s, output file: %s" % \
          (unit.uid, unit.execution_locations, unit.state,
           unit.exit_code,  unit.start_time, unit.stop_time,
           unit.description.output_staging[0]['target'])

    # Close automatically cancels the pilot(s).
    session.close()

# -----------------------------------------------------------------------------
