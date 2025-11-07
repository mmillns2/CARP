import numpy as np
import logging
import time
#from caen_felib import lib, device, error
from typing import Optional

from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QMainWindow,
    QPushButton,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QLabel,
    QApplication
)

from PySide6.QtCore import QTimer, QWaitCondition, QMutex, Signal, QThread, QObject, QCoreApplication

from caen_felib import lib, device, error

from core.tsqueue import TSQueue
from core.io import read_config_file
from core.logging import setup_logging
from felib.digitiser import Digitiser
from ui import oscilloscope


# --- Inherit QOject to allow Signals
class Controller(QObject):

    # --- signal that triggers AcquisitionWorker.stop() --- 
    stop_requested = Signal()
    # -----------------------------------------------------

    def __init__(self, 
                 display_buffer: TSQueue,
                 dig_config: Optional[str] = None, 
                 rec_config: Optional[str] = None,
                 parent=None):
        '''
        Initialise controller for GUI and digitiser
        '''
        super().__init__(parent=parent)

        # buffer shared between acquisition_worker and main threads
        self.display_buffer = display_buffer

        # Initialise buffer shared between acquisition_worker and logger threads
        self.write_buffer = TSQueue()

        # local flag shows if acquisition_worker is running
        # while running, check this flag rather than digitiser.isAcquiring
        self.acquisition_running = False  

        # Initialise logging and tracking
        setup_logging()
        self.tracker = Tracker()

        # digitiser connection first
        self.dig_config = dig_config
        self.rec_config = rec_config

        if dig_config is None:
            logging.warning("No digitiser configuration file provided. Digitiser will not be connected.")
            self.digitiser = None
        else:
            self.digitiser = self.connect_digitiser()

        # check digitiser connection, if valid set isConnected to True
        if self.digitiser is not None:
            self.digitiser.isConnected = True
            logging.info(f"Digitiser connected: {self.digitiser.URI}")
        else:
            logging.warning("Digitiser not connected.")


        # gui second
        self.app = QApplication([])
        self.main_window = oscilloscope.MainWindow(controller = self)

        self.fps_timer  = QTimer()
        self.fps_timer.timeout.connect(self.update_fps)
        self.spf = 1 # seconds per frame

        # --- add signal flag for stopping ---
        stop_requested = Signal()
        # ------------------------------------

        # worker third
        # if self.digitiser is not None and self.digitiser.isConnected:
        #     self.initialise_worker()

    def initialise_worker(self):
        '''
        Initialise the worker thread.
        This in turn should begin the data collection (I think?)
        '''
        
        # initialise digitiser
        # self.digitiser = self.connect_digitiser()

        # create thread to manage data output
        self.worker_wait_condition = QWaitCondition()
        self.acquisition_worker    = AcquisitionWorker(self.worker_wait_condition,
                                                       digitiser = self.digitiser,
                                                       display_buffer = self.display_buffer,
                                                       write_buffer = self.write_buffer)
        self.acquisition_thread    = QThread()

        # --- also move digitiser to acquisition_thread ---
        self.digitiser.moveToThread(self.acquisition_thread)
        # we don't move wait_condition - instead we wrap with mutex/locks
        # we don't move display/write buffers as they are already thread safe
        # -------------------------------------------------

        self.acquisition_worker.moveToThread(self.acquisition_thread)
        self.acquisition_thread.started.connect(self.acquisition_worker.run)
        self.acquisition_worker.data_ready.connect(self.data_handling)

        # --- connect stop_requested signal to acquisition_worker.stop() --- 
        self.stop_requested.connect(self.acquisition_worker.stop)
        # ------------------------------------------------------------------

        # when finished quit the thread and clean up
        self.acquisition_worker.finished.connect(self.acquisition_thread.quit)
        # self.acquisition_worker.finished.connect(self.acquisition_worker.deleteLater)
        # self.acquisition_thread.finished.connect(self.acquisition_thread.deleteLater)

        self.acquisition_thread.start()
        
        # --- update main thread local flag ---
        self.acquisition_running = True
        # -------------------------------------

    # --- new ---
    def stop_worker(self):
        '''
        Stops the worker thread and safely moves digitiser back to the main thread.
        Once initialise_worker() has been called, digitiser member functions should NOT
        be called from the main thread until stop_worker() has been called. 
        '''
        if not hasattr(self, "acquisition_thread"):
            return

        # tell the worker to stop safely using a signal
        self.stop_requested.emit()  # signal delivered to acquisition thread

        # wait for thread to finish
        self.acquisition_thread.quit()
        self.acquisition_thread.wait()

        # move objects back to main thread (so we can call their methods safely)
        # self.digitiser.moveToThread(QCoreApplication.instance().thread())
        # self.acquisition_worker.moveToThread(QCoreApplication.instance().thread())

        # clean up the thread object
        self.acquisition_thread.deleteLater()
        self.acquisition_worker.deleteLater()
        self.acquisition_thread = None


        # update main thread local flag
        self.acquisition_running = False


    def data_handling(self):
        '''
        Right now: Acquisition thread signals data_handling() when data is ready.
                   Main thread then calls acquisition_worker.data <-- race condition.
        Need to: Read data from a thread safe shared buffer.
                 Need a global (to controller & acquisition_worker) display buffer. 
        '''
        # visualise (and at some point, collect in a file)
        # wf_size, ADCs = self.acquisition_worker.data  # this needs to change

        # --- no race condition here ---
        wf_size, ADCs = self.display_buffer.pop_front()  
        # ------------------------------

        # save the data (PUT IT HERE) <-- don't save data here

        # update visuals
        self.main_window.screen.update_ch(np.arange(0, wf_size, dtype=wf_size.dtype), ADCs)
        
        # ping the tracker (make this optional)
        self.tracker.track(ADCs.nbytes)
        
        # prep the next thread
        # if self.digitiser.isAcquiring:  # change to local flag rather than digitiser flag
        if self.acquisition_running:
            self.worker_wait_condition.notify_one() # this is fine


    def update_fps(self):
        '''
        Update the FPS label in the GUI
        '''
        fps = 1 / self.spf
        self.main_window.stats_box.fps_label.setText(f"FPS: {fps:.2f}")

    def run_app(self):
        self.main_window.show()
        return self.app.exec()
    
    def connect_digitiser(self):
        '''
        Connect to the digitiser using the provided configuration file.
        This is a placeholder function and should be replaced with actual
        digitiser connection logic.
        '''

        # Load in configs
        dig_dict = read_config_file(self.dig_config)
        rec_dict = read_config_file(self.rec_config)
        
        if dig_dict is None:
            logging.error("Digitiser configuration file not found or invalid.")
            #raise ValueError("Digitiser configuration file not found or invalid.")
        else:
            digitiser = Digitiser(dig_dict)
            digitiser.connect()
            # Only add to the main window if it exists
            if hasattr(self, 'main_window'):
                self.main_window.control_panel.acquisition.update()

        # once connected, configure recording setup
        if rec_dict is None:
            logging.warning("No recording configuration file provided.")
        else:
            if (digitiser is not None) and digitiser.isConnected:
                digitiser.configure(rec_dict)
        return digitiser              
            
    # --- should never be called --- 
    def start_acquisition(self):
        '''
        Start the acquisition in multiple steps:
            - Start the digitiser acquisition based on whatever trigger
              settings are applied,
            - Initialise the data reader,
            - Initialise the output visuals.
        '''
        try:
            self.digitiser.start_acquisition()
            self.worker_wait_condition.wakeAll()
        except Exception as e:
            logging.exception('Failed to start acquisition.')
        #self.digitiser.start_acquisition()
        #self.trigger_and_record()
        
    # --- should never be called --- 
    def stop_acquisition(self):
        '''
        Simple stopping of acquisition, this will end the AcquisitionWorkers loop and terminate
        '''
        self.digitiser.isAcquiring = False

    # --- should never be called --- (currently never called) 
    def trigger_and_record(self):
        '''
        Apply whatever trigger is designated and record.
        Needs to also print occasionally to output.
        '''
        if self.digitiser.isAcquiring:
            evt_cnt = 0
            match self.digitiser.trigger_mode:
                case 'SWTRIG':
                    self.SW_record()
                case _:
                    logging.info(f'Trigger mode {self.trigger_mode} not currently implemented.')
                    self.stop_acquisition()    

        # check after running if isAcquiring is still enabled.
        if not self.digitiser.isAcquiring:
            self.digitiser.stop_acquisition()
            logging.info(f'Stopped acquisition.')


    # --- should never be called ---  (currently never called)
    def SW_record(self):
        # spam triggers as fast as possible here
        evt_counter = 0
        while self.digitiser.isAcquiring:
            self.digitiser.dig.cmd.SENDSWTRIGGER()

            try:
                self.digitiser.endpoint.read_data(100, self.digitiser.data) # timeout first number in ms
            except error.Error as ex:
                logging.exception("Error in readout:")
                if ex.code is error.ErrorCode.TIMEOUT:
                    continue
                if ex.code is error.ErrorCode.STOP:
                    break
                raise ex
        
            # ensure the input and trigger are acceptable (I think?)
            #assert self.data[3].value == 1 # VPROBE INPUT? I need to understand this
            #assert self.data[6].value == 1 # VPROBE TRIGGER?
            waveform_size = self.digitiser.data[7].value
            valid_sample_range = np.arange(0, waveform_size, dtype = waveform_size.dtype)

            # increase the event counter
            evt_counter += 1

            if (evt_counter % 100) == 0:
                self.main_window.screen.update_ch(valid_sample_range, (self.digitiser.data[3].value))



class AcquisitionWorker(QObject):

    data_ready = Signal()
    finished = Signal()

    def __init__(self, wait_condition, digitiser, display_buffer, write_buffer, parent=None):
        super().__init__(parent=parent)
        self.wait_condition = wait_condition
        self.digitiser = digitiser
        self.display_buffer = display_buffer  # thread safe queue
        self.write_buffer = write_buffer    # thread safe queue
        self.mutex = QMutex()
        # ensure on initial startup that you're not acquiring.
        self.digitiser.isAcquiring = False
        # --- add running flag ---
        self.is_running = False
        # ------------------------
    
    def run(self):
        self.is_running = True

        try:
            self.digitiser.start_acquisition()
            self.wait_condition.wakeAll()
        except Exception as e:
            logging.exception('Failed to start acquisition.')
            self.is_running = False
            self.finished.emit()
            return

        while self.is_running:
            print("run")
            self.mutex.lock()
            if not self.digitiser.isAcquiring:  # this needs checking - might need a signal/slot here
                self.wait_condition.wait(self.mutex)
            self.mutex.unlock()
            
            try:
                self.data = self.digitiser.acquire()
                self.display_buffer.push_back(self.data)
                self.write_buffer.push_back(self.data)
                self.data_ready.emit()  # signal controller to call data_handling()
                # should also signal to writer to write data to h5 file
            except Exception:
                logging.exception("Error during acquisition.")
                break

            QCoreApplication.processEvents()
        
        # if self.is_running:
        #     self.stop()

        # clean shutdown
        self.digitiser.stop_acquisition()
        self.finished.emit()
        print("run exiting")

    def stop(self):
        #if not self.is_running:
            print("called stop")
            self.mutex.lock()     # since wait_condition is still shared between threads 
            self.is_running = False
            # self.digitiser.stop_acquisition()
            self.wait_condition.wakeAll()
            self.mutex.unlock()
            # move objects back to main thread (so we can call their methods safely)
            self.digitiser.moveToThread(QCoreApplication.instance().thread())
            self.moveToThread(QCoreApplication.instance().thread())




#class Writer(QObject):




class Tracker:
    '''
    Tracking class that keeps track of:
        - number of collected events
        - speed at which data is being collected
    '''

    def __init__(self):
        self.start_time = time.perf_counter()
        self.bytes_ps   = 0
        self.events_ps  = 0
        self.last_time  = self.start_time

    def track(self, nbytes: int = 0):
        '''
        Tracker outputting the number of events that arrive per second
        '''
        self.events_ps += 1
        self.bytes_ps += nbytes

        t_check = time.perf_counter()
        if t_check - self.last_time >= 1.0:
            MB = self.bytes_ps / 1000000
            logging.info(f'|| {self.events_ps} events/sec || {MB:.2f} MB/sec ||')
            self.last_time = t_check
            self.bytes_ps = 0
            self.events_ps = 0
