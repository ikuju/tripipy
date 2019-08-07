#!/usr/bin/python3
"""
Driver for a trinamic tmc5130-bob on a raspberry pi using SPI.

This code is only raspberry pi specific because it uses pigpio, the vast majority of the code will work over any spi driver.

This used the 5160 example code (http://blog.trinamic.com/2018/02/19/stepper-motor-with-tmc5160/) as initial guidance but is mostly
written from info in the TMC5130 datasheet (https://www.trinamic.com/fileadmin/assets/Products/ICs_Documents/TMC5130_datasheet_Rev1.15.pdf)

"""
import logging
import pigpio
import time
import sys
from collections import OrderedDict
from enum import IntFlag, auto

import treedict
import trinamicDriver, tmc5130regs

from tmc5130regs import statusFlags as motorStatus

class regFlags(IntFlag):
    NONE        =0
    uptodate    =auto()

class appreg(treedict.Tree_dict):
    """
    The underlying driver provides access to all the trinamic chip registers under the node 'chipregs'.
    
    This class (and those inheriting from it) enable the chipreg values to be converted to more convenient values
    (such as rpm).
    
    These are used where the underlying chip register is always in direct correspondence to the value here, so the chip
    register IS the reference value - the value in this class is always computed from the chip register value.
    """
    def __init__(self, chipreg, logacts = ('constructors', 'content'), **kwargs):
        """
        chipreg:  path to the chip register this value is based on
        """
        super().__init__(**kwargs)
        self.chipreg=self[chipreg]

class appregVelocity(appreg):
    """
    for chipregisters representing velocity, using the standard convertion.
    """
    def getCurrent(self):
        return self['../..'].VREGtoRPM(self.chipreg.getCurrent())

    def setVal(self, value):
        self.chipreg.setValue(self['../..'].RPMtoVREG(value))

class appregPosn(appreg):
    """
    converts position regs (XACTUAL or XTARGET to a meanigful value
    """
    def __init__(self, chipreg='../../chipregs/XACTUAL', **kwargs):
        super().__init__(chipreg=chipreg, **kwargs)

    def getCurrent(self):
        return self.chipreg.getCurrent()/self['../uStepsPerRev'].getCurrent()

    def setVal(self, value):
        self.chipreg.setValue(round(value*self['../uStepsPerRev'].getCurrent()))

class appval(treedict.Tree_dict):
    """
    a base class for settings / states that need to be processed to do things to the chip. 
    
    e.g. a value in rpm is converted to / from a chip register value using info about the clock frequency and the motor

    This simplest case just records a value
    """
    def __init__(self, value=None, **kwargs):
        super().__init__(**kwargs)
        self.setVal(value)

    def getCurrent(self):
        return self.curval

    def setVal(self, value):
        self.curval=value

class uStepsPR(appval):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.setVal(self['../stepsPerRev'].getCurrent()*self['../uSteps'].getCurrent())

motor28BYJ_48=(
    {'_cclass': appval,     'name': 'stepsPerRev'   , 'value': 2048/12},    # motor and gearbox with 12:1 speedup to second hand
    {'_cclass': appval,     'name': 'maxrpm'        , 'value': 220},        # 1 rpm is 1 rotation of the second hand per minute
    {'_cclass': appval,     'name': 'uSteps'        , 'value': 256},        # microsteps per full step - 256 unless you do weird stuff to the chip
    {'_cclass': uStepsPR,   'name': 'uStepsPerRev'},                        # calculated from stepsPerRev and uStepsPerRev
    {'_cclass': appregPosn, 'name': 'posn'},
    {'_cclass': appregPosn, 'name': 'target', 'chipreg':'../../chipregs/XTARGET'},
    {'_cclass': appregVelocity, 'name': 'rpmnow', 'chipreg': '../../chipregs/VACTUAL'},
)
regorder=('stepsPerRev', 'maxrpm')

class tmc5130(trinamicDriver.TrinamicDriver):
    """
    A class specific to the TMC5130 chip. The detailed register definitions are held in the tmc5130regs module.
    """
    def __init__(self, clockfrequ=15000000, settings=motor28BYJ_48, pigio=None, loglvl=logging.DEBUG):
        """
        sets up a motor driver for the trinamic tm,c5130
        
        clockfrequ   : clock frequency (generated by the RPi and passed to the chip, 10MHz - 16MHz recommended in manual
        
        settings     : a bunch of settings for the registers in the driver chip and some for this driver that override the default values
        
        pigio        : an instance of pigpio to use for communication with the trinamic chip, if None an instance is created
        
        loglvl       : sets the level used as the minimum for debug calls (no debug calls are made for levels below this value to improve
                        performance)
        """
        logging.basicConfig(
            level=logging.DEBUG if isinstance(loglvl, str) else loglvl, 
            format='%(asctime)s %(levelname)7s (%(process)d)%(threadName)12s  %(module)s.%(funcName)s: %(message)s',
            datefmt="%H:%M:%S")
        if pigio is None:
            self.pg=pigpio.pi()
            self.mypio=True
        else:
            self.pg=pigio
            self.mypio=False
        if not self.pg.connected:
            logging.getLogger().critical("pigpio daemon does not appear to be running")
            sys.exit(1)
        self.clockfrequ=clockfrequ
        self.tconst=self.clockfrequ/2**24
        super().__init__(name='fred', parent=None, app=None, clockfrequ=self.clockfrequ, datarate=1000000, pigp=self.pg,
                motordef=tmc5130regs.tmc5130, drvenpin=12, spiChannel=1, loglvl=loglvl )
        self.makeChild(_cclass=treedict.Tree_dict, name='settings', childdefs=settings)
#        self.maxV=round(self.RPMtoVREG(self['settings/maxrpm'].getCurrent()))
        regsettings=OrderedDict((   # base set of register values to get started
                ('GSTAT',0),
                ('GCONF',4),
                ('CHOPCONF', 0x000100C3),
                ('IHOLD_IRUN', 0x00080F0A),
                ('TPOWERDOWN', 0x0000000A),
                ('TPWMTHRS', 0x000001F4),
                ('VSTART', 30),
                ('A1', 1500),
                ('V1', 100000),
                ('AMAX', 1000),
                ('VMAX', round(self.RPMtoVREG(self['settings/maxrpm'].getCurrent()))),
                ('DMAX', 1100),
                ('D1', 600),
                ('VSTOP', 40),
                ('RAMPMODE',0)
                 ))
        regactions='RUWWWWWWWWWWWWW'
        assert len(regsettings)==len(regactions)
        self.readWriteMultiple(regsettings,regactions)

    def RPMtoVREG(self, rpm):
        """
        calculates reg value (e.g. VMAX) for a given rpm

        vreg=(rpm*ustepsperrev) / (60*self.tconst)
        """
        v1= (rpm*self['settings/uStepsPerRev'].getCurrent()/60) / self.tconst
        return v1

    def VREGtoRPM(self, regval):
        """
        inverse of RPMtoVREG.
        
        rpm=(vreg*60*tconst) / ustepsperrev
        """
        return regval*60*self.tconst/self['settings/uStepsPerRev'].getCurrent()

    def posToREG(self, posn):
        return round(self['settings/uStepsPerRev'].getCurrent()*posn)

    def readState(self, statedict):
        """
        returns the current value of a setting or register for this motor
        
        statedict:  a dict where each entry has a key corresponding to the desired item.
                    the values are ignored
                    - or -
                    a list of names of items, in which case a dict is returned with the value for each item
        
        returns the original dict (if a dict passed) or a new dict (if list passed), the values will be the setting or register value (at the time of the call)
        """
        res=statedict if isinstance(statedict, dict) else {k:None for k in statedict}
        for k in res.keys():
            if k in self.settings:
                res[k]=self.settings[k]
        return res

    def wait_reached(self, ticktime=.5):
        time.sleep(ticktime)
        reads={'VACTUAL':0, 'XACTUAL':0, 'XTARGET':0, 'GSTAT':0, 'RAMPSTAT':0}
        self.readWriteMultiple(reads, 'R')
        while not motorStatus.at_position in self.status:
            print('loc    {location:9.2f}   chipVelocity  {velocity:9.2f}'.format(location=reads['XACTUAL']/self['settings/uStepsPerRev'].getCurrent(), velocity=reads['VACTUAL']))
            print('ramp status: %s' % self.status)
            time.sleep(ticktime)
            self.readWriteMultiple(reads, 'R')
        self.enableOutput(False)
        print('target %9.4f reached (%d), status: %x, ramp status %s' % (reads['XACTUAL']/self['settings/uStepsPerRev'].getCurrent(), reads['XACTUAL'], self.status, reads['RAMPSTAT']))

    def waitStop(self, ticktime):
        time.sleep(ticktime)
        while self.readInt('VACTUAL') != 0:
            time.sleep(ticktime)

    def goto(self, targetpos, speed=None):
        regupdates=OrderedDict((
            ('VMAX', round(self.RPMtoVREG(self['settings/maxrpm'].getCurrent() if speed is None else speed))),
            ('XTARGET', round(self['settings/uStepsPerRev'].getCurrent()*targetpos)),
            ('RAMPMODE',0),
             ))
        self.enableOutput(True)
        self.readWriteMultiple(regupdates,'W')
        print('requested %d, recorded %d' % (regupdates['VMAX'], self['chipregs/VMAX'].curval))

    def setspeed(self, speed):
        regupdates=OrderedDict((
            ('VMAX', round(self.RPMtoVREG(abs(speed)))),
            ('RAMPMODE',1 if speed >=0 else 2),
            ))
        self.enableOutput(True)
        self.readWriteMultiple(regupdates,'W')

    def stop(self):
        self.writeInt('XTARGET', self.readInt('XACTUAL'))
        self.writeInt('VMAX', round(self.RPMtoVREG(self['settings/maxrpm'].getCurrent())))
        self.writeInt('RAMPMODE',0)
        self.waitStop(ticktime=.1)
        self.enableOutput(False)

    def close(self):
        super().close()
        if self.mypio:
            self.pg.stop()
        self.pg=None
