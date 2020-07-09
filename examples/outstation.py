import logging
import sys
import os
import cppyy
import setup_cppyy

from cppyy.gbl import opendnp3
from cppyy.gbl.opendnp3 import (
    levels,
    IChannelListener,
    ILogHandler,
    ConsoleLogger,
    DNP3Manager,
    ChannelRetry,
    ServerAcceptMode,
    TimeDuration,
    PrintingChannelListener,
    IPEndpoint,
    ICommandHandler,
    IOutstationApplication,
    DatabaseConfig,
    EventBufferConfig,
    OutstationStackConfig,
    BinaryConfig,
    AnalogConfig,
    PointClass,
    StaticAnalogVariation,
    StaticBinaryVariation,
    EventAnalogVariation,
    EventBinaryVariation,
    SuccessCommandHandler,
    UpdateBuilder,
    RestartMode
)
LOG_LEVELS = levels.NORMAL | levels.ALL_COMMS
LOCAL_IP = "0.0.0.0"
PORT = 20005

stdout_stream = logging.StreamHandler(sys.stdout)
stdout_stream.setFormatter(logging.Formatter('%(asctime)s\t%(name)s\t%(levelname)s\t%(message)s'))

_log = logging.getLogger(__name__)
_log.addHandler(stdout_stream)
_log.setLevel(logging.DEBUG)


class OutstationApplication(IOutstationApplication):
    """
        Interface for all outstation callback info except for control requests.

        DNP3 spec section 5.1.6.2:
            The Application Layer provides the following services for the DNP3 User Layer in an outstation:
                - Notifies the DNP3 User Layer when action requests, such as control output,
                  analog output, freeze and file operations, arrive from a master.
                - Requests data and information from the outstation that is wanted by a master
                  and formats the responses returned to a master.
                - Assures that event data is successfully conveyed to a master (using
                  Application Layer confirmation).
                - Sends notifications to the master when the outstation restarts, has queued events,
                  and requires time synchronization.

        DNP3 spec section 5.1.6.3:
            The Application Layer requires specific services from the layers beneath it.
                - Partitioning of fragments into smaller portions for transport reliability.
                - Knowledge of which device(s) were the source of received messages.
                - Transmission of messages to specific devices or to all devices.
                - Message integrity (i.e., error-free reception and transmission of messages).
                - Knowledge of the time when messages arrive.
                - Either precise times of transmission or the ability to set time values
                  into outgoing messages.
    """

    outstation = []

    def __init__(self):
        super(OutstationApplication, self).__init__()

        _log.debug('Configuring the DNP3 stack.')
        self.stack_config = self.configure_stack()

        _log.debug('Configuring the outstation database.')
        self.configure_database(self.stack_config.database)

        _log.debug('Creating a DNP3Manager.')
        threads_to_allocate = 1

        self.log_handler = ConsoleLogger(False).Create()              # (or use this during regression testing)
        self.manager = DNP3Manager(threads_to_allocate, self.log_handler)
        self.server_accept_mode = ServerAcceptMode.CloseNew

        _log.debug('Creating the DNP3 channel, a TCP server.')
        self.retry_parameters = ChannelRetry(TimeDuration.Seconds(1), TimeDuration.Seconds(300)).Default()

        self.listener = PrintingChannelListener().Create()       # (or use this during regression testing)
        self.channel = self.manager.AddTCPServer("server",
                                                 LOG_LEVELS,
                                                 self.server_accept_mode,
                                                 IPEndpoint(LOCAL_IP,PORT),
                                                 self.listener)

        _log.debug('Adding the outstation to the channel.')
        self.command_handler = OutstationCommandHandler()
        self.outstation = self.channel.AddOutstation("outstation", self.command_handler, self, self.stack_config)

        # Put the Outstation singleton in OutstationApplication so that it can be used to send updates to the Master.
        OutstationApplication.set_outstation(self.outstation)

        _log.debug('Enabling the outstation. Traffic will now start to flow.')
        self.outstation.Enable()

    @staticmethod
    def configure_stack():
        """Set up the OpenDNP3 configuration."""
        stack_config = OutstationStackConfig(DatabaseConfig(10))
        stack_config.outstation.eventBufferConfig = EventBufferConfig().AllTypes(10)
        stack_config.outstation.params.allowUnsolicited = True
        stack_config.link.LocalAddr = 1
        stack_config.link.RemoteAddr = 10
        stack_config.link.KeepAliveTimeout = TimeDuration().Max()
        return stack_config

    @staticmethod
    def configure_database(db_config):
        """
            Configure the Outstation's database of input point definitions.

            Configure two Analog points (group/variation 30.1) at indexes 1 and 2.
            Configure two Binary points (group/variation 1.2) at indexes 1 and 2.
        """
        db_config.analog_input[1].clazz = PointClass.Class2
        db_config.analog_input[1].svariation = StaticAnalogVariation.Group30Var1
        db_config.analog_input[1].evariation = EventAnalogVariation.Group32Var7
        db_config.analog_input[2].clazz = PointClass.Class2
        db_config.analog_input[2].svariation = StaticAnalogVariation.Group30Var1
        db_config.analog_input[2].evariation = EventAnalogVariation.Group32Var7
        db_config.binary_input[1].clazz = PointClass.Class2
        db_config.binary_input[1].svariation = StaticBinaryVariation.Group1Var2
        db_config.binary_input[1].evariation = EventBinaryVariation.Group2Var2
        db_config.binary_input[2].clazz = PointClass.Class2
        db_config.binary_input[2].svariation = StaticBinaryVariation.Group1Var2
        db_config.binary_input[2].evariation = EventBinaryVariation.Group2Var2

    def shutdown(self):
        """
            Execute an orderly shutdown of the Outstation.

            The debug messages may be helpful if errors occur during shutdown.
        """
        _log.debug('Exiting application...')
        _log.debug('Shutting down outstation...')
        OutstationApplication.set_outstation(None)
        _log.debug('Shutting down stack config...')
        self.stack_config = None
        _log.debug('Shutting down channel...')
        self.channel = None
        _log.debug('Shutting down DNP3Manager...')
        self.manager.Shutdown()
        self.manager = None

        

    @classmethod
    def set_outstation(cls, outstn):
        """Get the singleton instance of IOutstation."""
        cls.outstation = outstn


    @classmethod
    def get_outstation(cls):
        """
            Set the singleton instance of IOutstation, as returned from the channel's AddOutstation call.

            Making IOutstation available as a singleton allows other classes (e.g. the command-line UI)
            to send commands to it -- see apply_update().
        """
        return cls.outstation

    # Overridden method
    def ColdRestartSupport(self):
        """Return a RestartMode enumerated value indicating whether cold restart is supported."""
        _log.debug('In OutstationApplication.ColdRestartSupport')
        return RestartMode.UNSUPPORTED

    # Overridden method
    def GetApplicationIIN(self):
        """Return the application-controlled IIN field."""
        application_iin = ApplicationIIN()
        application_iin.configCorrupt = False
        application_iin.deviceTrouble = False
        application_iin.localControl = False
        application_iin.needTime = False
        # Just for testing purposes, convert it to an IINField and display the contents of the two bytes.
        iin_field = application_iin.ToIIN()
        _log.debug('OutstationApplication.GetApplicationIIN: IINField LSB={}, MSB={}'.format(iin_field.LSB,
                                                                                             iin_field.MSB))
        return application_iin

    # Overridden method
    def SupportsAssignClass(self):
        _log.debug('In OutstationApplication.SupportsAssignClass')
        return False

    # Overridden method
    def SupportsWriteAbsoluteTime(self):
        _log.debug('In OutstationApplication.SupportsWriteAbsoluteTime')
        return False

    # Overridden method
    def SupportsWriteTimeAndInterval(self):
        _log.debug('In OutstationApplication.SupportsWriteTimeAndInterval')
        return False

    # Overridden method
    def WarmRestartSupport(self):
        """Return a RestartMode enumerated value indicating whether a warm restart is supported."""
        _log.debug('In OutstationApplication.WarmRestartSupport')
        return RestartMode.UNSUPPORTED

    @classmethod
    def process_point_value(cls, command_type, command, index, op_type):
        """
            A PointValue was received from the Master. Process its payload.

        :param command_type: (string) Either 'Select' or 'Operate'.
        :param command: A ControlRelayOutputBlock or else a wrapped data value (AnalogOutputInt16, etc.).
        :param index: (integer) DNP3 index of the payload's data definition.
        :param op_type: An OperateType, or None if command_type == 'Select'.
        """
        _log.debug('Processing received point value for index {}: {}'.format(index, command))

    def apply_update(self, value, index):
        """
            Record an opendnp3 data value (Analog, Binary, etc.) in the outstation's database.

            The data value gets sent to the Master as a side-effect.

        :param value: An instance of Analog, Binary, or another opendnp3 data value.
        :param index: (integer) Index of the data definition in the opendnp3 database.
        """
        _log.debug('Recording {} measurement, index={}, value={}'.format(type(value).__name__, index, value.value))
        builder = UpdateBuilder()
        builder.Update(value, index)
        update = builder.Build()
        OutstationApplication.get_outstation().Apply(update)


class OutstationCommandHandler(ICommandHandler):
    """
        Override ICommandHandler in this manner to implement application-specific command handling.

        ICommandHandler implements the Outstation's handling of Select and Operate,
        which relay commands and data from the Master to the Outstation.
    """
    def __init__(self):
        super(OutstationCommandHandler, self).__init__()

    def Begin(self):
        _log.debug('In OutstationCommandHandler.Begin')

    def End(self):
        _log.debug('In OutstationCommandHandler.End')

    def Select(self, command, index):
        """
            The Master sent a Select command to the Outstation. Handle it.

        :param command: ControlRelayOutputBlock,
                        AnalogOutputInt16, AnalogOutputInt32, AnalogOutputFloat32, or AnalogOutputDouble64.
        :param index: int
        :return: CommandStatus
        """
        OutstationApplication.process_point_value('Select', command, index, None)
        return CommandStatus.SUCCESS

    def Operate(self, command, index, op_type):
        """
            The Master sent an Operate command to the Outstation. Handle it.

        :param command: ControlRelayOutputBlock,
                        AnalogOutputInt16, AnalogOutputInt32, AnalogOutputFloat32, or AnalogOutputDouble64.
        :param index: int
        :param op_type: OperateType
        :return: CommandStatus
        """
        OutstationApplication.process_point_value('Operate', command, index, op_type)
        return CommandStatus.SUCCESS


class AppChannelListener(IChannelListener):
    """
        Override IChannelListener in this manner to implement application-specific channel behavior.
    """

    def __init__(self):
        super(AppChannelListener, self).__init__()

    def OnStateChange(self, state):
        _log.debug('In AppChannelListener.OnStateChange: state={}'.format(state))


class MyLogger(ILogHandler):
    """
        Override ILogHandler in this manner to implement application-specific logging behavior.
    """

    def __init__(self):
        super(MyLogger, self).__init__()

    def Log(self, entry):
        filters = entry.filters.GetBitfield()
        location = entry.location.rsplit('/')[-1] if entry.location else ''
        message = entry.message
        _log.debug('Log\tfilters={}\tlocation={}\tentry={}'.format(filters, location, message))


def main():
    """The Outstation has been started from the command line. Execute ad-hoc tests if desired."""
    app = OutstationApplication()
    _log.debug('Initialization complete. In command loop.')
    # Ad-hoc tests can be inserted here if desired. See outstation_cmd.py for examples.
    app.shutdown()
    _log.debug('Exiting.')
    exit()


if __name__ == '__main__':
    main()