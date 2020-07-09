import logging
import sys
import time
import cppyy
import setup_cppyy
# import pdb
# import cppyy.ll
# cppyy.ll.set_signals_as_exception(True)

from cppyy.gbl import opendnp3
from cppyy.gbl.opendnp3 import ( 
    levels, 
    DNP3Manager, 
    ConsoleLogger, 
    ChannelRetry, 
    IPEndpoint, 
    PrintingChannelListener, 
    MasterStackConfig, 
    TimeDuration, 
    PrintingSOEHandler, 
    DefaultMasterApplication, 
    HeaderInfo, 
    ResponseInfo, 
    ISOEHandler, 
    ICollection, 
    Indexed, 
    Binary, 
    DoubleBitBinary, 
    Analog, 
    Counter, 
    FrozenCounter, 
    BinaryOutputStatus, 
    AnalogOutputStatus, 
    OctetString, 
    TimeAndInterval, 
    BinaryCommandEvent, 
    AnalogCommandEvent, 
    DNPTime,
    ClassField,
    PrintingCommandResultCallback,
    IChannelListener,
    LogLevels,
    ILogHandler,
    UTCTimestamp,
    IMasterApplication,
    ICollection,
    Indexed
    )

from visitors import *

FILTERS = levels.NORMAL | levels.ALL_APP_COMMS
HOST = "127.0.0.1"
LOCAL = "0.0.0.0"
PORT = 20000

stdout_stream = logging.StreamHandler(sys.stdout)
stdout_stream.setFormatter(logging.Formatter('%(asctime)s\t%(name)s\t%(levelname)s\t%(message)s'))

_log = logging.getLogger(__name__)
_log.addHandler(stdout_stream)
_log.setLevel(logging.DEBUG)

class MyMaster:
    """
        Interface for all master application callback info except for measurement values.

        DNP3 spec section 5.1.6.1:
            The Application Layer provides the following services for the DNP3 User Layer in a master:
                - Formats requests directed to one or more outstations.
                - Notifies the DNP3 User Layer when new data or information arrives from an outstation.

        DNP spec section 5.1.6.3:
            The Application Layer requires specific services from the layers beneath it.
                - Partitioning of fragments into smaller portions for transport reliability.
                - Knowledge of which device(s) were the source of received messages.
                - Transmission of messages to specific devices or to all devices.
                - Message integrity (i.e., error-free reception and transmission of messages).
                - Knowledge of the time when messages arrive.
                - Either precise times of transmission or the ability to set time values
                  into outgoing messages.
    """
    def __init__(self,
                 log_handler=ConsoleLogger(False).Create(),
                 listener=PrintingChannelListener().Create(),
                 soe_handler=PrintingSOEHandler().Create(),
                 master_application=DefaultMasterApplication().Create(),
                 stack_config=None):

        threads_to_allocate = 1
        self.log_handler = log_handler
        self.master_application = master_application
        self.listener = listener
        self.soe_handler = soe_handler

        _log.debug('Creating a DNP3Manager.')
        self.manager = DNP3Manager(threads_to_allocate, self.log_handler)
        _log.debug('Creating the DNP3 channel, a TCP client.')
        self.channel = self.manager.AddTCPClient("tcpClient", FILTERS, ChannelRetry.Default(), {IPEndpoint("127.0.0.1", 20000)}, "0.0.0.0", PrintingChannelListener.Create())
        
        self.master_application = master_application
        _log.debug('Configuring the DNP3 stack.')
        
        self.stack_config = MasterStackConfig()
        self.stack_config.master.responseTimeout = TimeDuration().Seconds(2)
        self.stack_config.link.LocalAddr = 10
        self.stack_config.link.RemoteAddr = 1

        _log.debug('Adding the master to the channel.')

        self.master = self.channel.AddMaster("master",
                                   self.soe_handler,
                                   self.master_application,
                                   self.stack_config)

        _log.debug('Configuring some scans (periodic reads).')

        self.integrity_scan = self.master.AddClassScan(ClassField.AllClasses(), TimeDuration.Minutes(1), self.soe_handler)

        self.exception_scan = self.master.AddClassScan(ClassField(ClassField.CLASS_1), TimeDuration.Seconds(5), self.soe_handler)

        _log.debug('Enabling the master. At this point, traffic will start to flow between the Master and Outstations.')
        self.master.Enable()
        time.sleep(5)

    def send_direct_operate_command(self, command, index, callback=PrintingCommandResultCallback.Get(),
                                    config=opendnp3.TaskConfig.Default()):
        """
            Direct operate a single command

        :param command: command to operate
        :param index: index of the command
        :param callback: callback that will be invoked upon completion or failure
        :param config: optional configuration that controls normal callbacks and allows the user to be specified for SA
        """
        self.master.DirectOperate(command, index, callback, config)

    def send_direct_operate_command_set(self, command_set, callback=PrintingCommandResultCallback.Get(),
                                        config=opendnp3.TaskConfig.Default()):
        """
            Direct operate a set of commands

        :param command_set: set of command headers
        :param callback: callback that will be invoked upon completion or failure
        :param config: optional configuration that controls normal callbacks and allows the user to be specified for SA
        """
        self.master.DirectOperate(command_set, callback, config)

    def send_select_and_operate_command(self, command, index, callback=PrintingCommandResultCallback.Get(),
                                        config=opendnp3.TaskConfig.Default()):
        """
            Select and operate a single command

        :param command: command to operate
        :param index: index of the command
        :param callback: callback that will be invoked upon completion or failure
        :param config: optional configuration that controls normal callbacks and allows the user to be specified for SA
        """
        self.master.SelectAndOperate(command, index, callback, config)

    def send_select_and_operate_command_set(self, command_set, callback=PrintingCommandResultCallback.Get(),
                                            config=opendnp3.TaskConfig.Default()):
        """
            Select and operate a set of commands

        :param command_set: set of command headers
        :param callback: callback that will be invoked upon completion or failure
        :param config: optional configuration that controls normal callbacks and allows the user to be specified for SA
        """
        self.master.SelectAndOperate(command_set, callback, config)

    def shutdown(self):
        del self.integrity_scan
        del self.exception_scan
        del self.master
        del self.channel
        self.manager.Shutdown()

class SOEHandler(opendnp3.ISOEHandler):
    """
        Override ISOEHandler in this manner to implement application-specific sequence-of-events behavior.

        This is an interface for SequenceOfEvents (SOE) callbacks from the Master stack to the application layer.
    """

    def __init__(self):
        super(SOEHandler, self).__init__()

    def Process(self, info, values):
        """
            Process measurement data.

        :param info: HeaderInfo
        :param values: A collection of values received from the Outstation (various data types are possible).
        """
        visitor_class_types = {
            ICollection(Indexed(Binary)): VisitorIndexedBinary,
            opendnp3.ICollectionIndexedDoubleBitBinary: VisitorIndexedDoubleBitBinary,
            opendnp3.ICollectionIndexedCounter: VisitorIndexedCounter,
            opendnp3.ICollectionIndexedFrozenCounter: VisitorIndexedFrozenCounter,
            opendnp3.ICollectionIndexedAnalog: VisitorIndexedAnalog,
            opendnp3.ICollectionIndexedBinaryOutputStatus: VisitorIndexedBinaryOutputStatus,
            opendnp3.ICollectionIndexedAnalogOutputStatus: VisitorIndexedAnalogOutputStatus,
            opendnp3.ICollectionIndexedTimeAndInterval: VisitorIndexedTimeAndInterval
        }
        visitor_class = visitor_class_types[type(values)]
        visitor = visitor_class()
        values.Foreach(visitor)
        for index, value in visitor.index_and_value:
            log_string = 'SOEHandler.Process {0}\theaderIndex={1}\tdata_type={2}\tindex={3}\tvalue={4}'
            _log.debug(log_string.format(info.gv, info.headerIndex, type(values).__name__, index, value))

    def BeginFragment(self):
        _log.debug('In SOEHandler.BeginFragment')

    def EndFragment(self):
        _log.debug('In SOEHandler.EndFragment')


class MyLogger(ILogHandler):
    """
        Override ILogHandler in this manner to implement application-specific logging behavior.
    """

    def __init__(self):
        super(MyLogger, self).__init__()

    def log(self, module, id, level, location, message):
        pass
        #_log.debug('LOG\tentry={}'.format(message))

class AppChannelListener(IChannelListener):
    """
        Override IChannelListener in this manner to implement application-specific channel behavior.
    """

    def __init__(self):
        super(AppChannelListener, self).__init__()

    def OnStateChange(self, state):
        _log.debug('In AppChannelListener.OnStateChange: state={}'.format(opendnp3.ChannelStateToString(state)))

    def Start(self):
        _log.debug('In SOEHandler.Start')

    def End(self):
        _log.debug('In SOEHandler.End')


class MasterApplication(IMasterApplication):
    def __init__(self):
        super(MasterApplication, self).__init__()

    # Overridden method
    def AssignClassDuringStartup(self):
        _log.debug('In MasterApplication.AssignClassDuringStartup')
        return False

    # Overridden method
    def OnClose(self):
        _log.debug('In MasterApplication.OnClose')

    # Overridden method
    def OnOpen(self):
        _log.debug('In MasterApplication.OnOpen')

    # Overridden method
    def OnReceiveIIN(self, iin):
        _log.debug('In MasterApplication.OnReceiveIIN')

    # Overridden method
    def OnTaskComplete(self, info):
        _log.debug('In MasterApplication.OnTaskComplete')

    def Now(self):
        _log.debug('In MasterApplication.Now')

        return UTCTimestamp()

    # Overridden method
    def OnTaskStart(self, type, id):
        _log.debug('In MasterApplication.OnTaskStart')


def collection_callback(result=None):
    """
    :type result: opendnp3.CommandPointResult
    """
    print("Header: {0} | Index:  {1} | State:  {2} | Status: {3}".format(
        result.headerIndex,
        result.index,
        opendnp3.CommandPointStateToString(result.state),
        opendnp3.CommandStatusToString(result.status)
    ))


def command_callback(result=None):
    """
    :type result: opendnp3.ICommandTaskResult
    """
    print("Received command result with summary: {}".format(opendnp3.TaskCompletionToString(result.summary)))
    result.ForeachItem(collection_callback)


def restart_callback(result=opendnp3.RestartOperationResult()):
    if result.summary == opendnp3.TaskCompletion.SUCCESS:
        print("Restart success | Restart Time: {}".format(result.restartTime.GetMilliseconds()))
    else:
        print("Restart fail | Failure: {}".format(opendnp3.TaskCompletionToString(result.summary)))


def main():
    """The Master has been started from the command line. Execute ad-hoc tests if desired."""
    # app = MyMaster()
    app = MyMaster(#log_handler=MyLogger(), # This is currently broken.  Not sure why at this point.
                   listener=AppChannelListener(),
                   soe_handler=SOEHandler(),
                   master_application=MasterApplication()
                   )
    _log.debug('Initialization complete. In command loop.')
    # Ad-hoc tests can be performed at this point. See master_cmd.py for examples.
    app.shutdown()
    _log.debug('Exiting.')
    exit()


if __name__ == '__main__':
    main()
