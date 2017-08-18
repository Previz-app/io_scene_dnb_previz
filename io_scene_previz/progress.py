import datetime
import getpass
import platform
import queue
import random
import sys
import time
import threading
import traceback

import pyperclip

import addon_utils
import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, path_reference_mode


def id_generator():
    id = -1
    while True:
        id += 1
        yield id
ids = id_generator()


class TasksRunner(object):
    def __init__(self, keep_finished_task_timeout = 2):
        self.keep_finished_task_timeout = keep_finished_task_timeout

        self.tasks = {}
        self.on_task_changed = []

    def add_task(self, task):
        id = next(ids)
        task.tasks_runner = self
        self.tasks[id] = task

        task.run()

        if len(self.tasks) == 1:
            bpy.ops.export_scene.previz_manage_queue()

        return id

    def tick(self):
        for task in self.tasks:
            task.tick()
        self.remove_finished_tasks()

    def cancel(self):
        for task in [t for t in self.tasks if t.is_cancelable]:
            task.cancel()

    def remove_finished_tasks(self):
        def is_timed_out(task):
            return task.status in (DONE, CANCELED) \
                   and (time.time() - task.finished_time) > self.keep_finished_task_timeout
        ids = [id for id, task in self.tasks.items() if is_timed_out(task)]
        for id in ids:
            self.remove_task(id)
            self.notify_change(None)

    def foreach_task(self, func):
        for task in self.tasks.values():
            func(task)

    @property
    def is_empty(self):
        return len(self.tasks) == 0

    def remove_task(self, task_id):
        task = self.tasks[task_id]
        if not task.is_finished:
            msg = 'Cannot remove unfinished task {!r}'.format(task.label)
            raise RuntimeError(msg)
        del self.tasks[task_id]

    def notify_change(self, task):
        for cb in self.on_task_changed:
            cb(self, task)


tasks_runner = None

IDLE = 'idle'
STARTING = 'starting'
RUNNING = 'running'
DONE = 'done'
CANCELING = 'canceling'
CANCELED = 'canceled'
ERROR = 'error'


class Task(object):
    def __init__(self):
        self.label = 'label'
        self.status = IDLE
        self.state = 'state'
        self.error = None
        self.progress = None
        self.finished_time = None
        self.tasks_runner = None

    def run(self):
        self.state = 'Running'
        self.status = RUNNING
        self.notify()

    def canceling(self):
        self.state = 'Canceling'
        self.status = CANCELING
        self.notify()

    def canceled(self):
        self.finished_time = time.time()
        self.state = 'Canceled'
        self.status = CANCELED
        self.notify()

    def done(self):
        self.finished_time = time.time()
        self.state = 'Done'
        self.status = DONE
        self.notify()

    def set_error(self, exc_info):
        self.finished_time = time.time()
        self.error = exc_info
        self.state = 'Error'
        self.status = ERROR
        self.notify()

    @property
    def is_cancelable(self):
        return hasattr(self, cancel)

    @property
    def is_finished(self):
        return self.status in (DONE, CANCELED, ERROR)

    def tick(self):
        pass

    def notify(self):
        self.tasks_runner.notify_change(self)


class DebugSyncTask(Task):
    def __init__(self):
        Task.__init__(self)

    def run(self):
        super().run()
        for ms in range(0, 510, 100):
            s = ms / 1000
            time.sleep(s)
            self.label = 'task {}'.format(s*2)
            self.progress = s*2
            self.notify()
        self.done()


REQUEST_CANCEL = 0
RESPOND_CANCELED = 1
TASK_DONE = 2
TASK_UPDATE = 3
TASK_ERROR = 4

class DebugAsyncTask(Task):
    def __init__(self):
        Task.__init__(self)

        self.queue_to_worker = queue.Queue()
        self.queue_to_main = queue.Queue()
        self.thread = threading.Thread(target=DebugAsyncTask.thread_run,
                                       args=(self.queue_to_worker,
                                             self.queue_to_main))

    def run(self):
        print('MAIN: Starting thread')
        self.thread.start()
        print('MAIN: Started thread')

    def cancel(self):
        self.canceling()
        self.queue_to_worker.put((REQUEST_CANCEL, None))

    @staticmethod
    def thread_run(queue_to_worker, queue_to_main):
        print('THREAD: Starting')
        try:
            for i in range(1, 11):
                while not queue_to_worker.empty():
                    msg, data = queue_to_worker.get()
                    if msg == REQUEST_CANCEL:
                        queue_to_main.put((RESPOND_CANCELED, None))
                        queue_to_worker.task_done()
                        return

                s = random.random()/2
                msg = (i, s)
                queue_to_main.put((TASK_UPDATE, msg))
                print('THREAD: Sleep {} {:.2}'.format(*msg))
                time.sleep(s)
            queue_to_main.put((TASK_DONE, None))
        except Exception as err:
            print('****** CAUGHT')
            queue_to_main.put((TASK_ERROR, sys.exc_info()))
        finally:
            print('THREAD: Stopping')

    def tick(self):
        #print('DebugAsyncTask.tick')
        while not self.queue_to_main.empty():
            msg, data = self.queue_to_main.get()
            print('msg', msg)
            print('is_finished', self.is_finished)

            if not self.is_finished:
                if msg == RESPOND_CANCELED:
                    self.finished_time = time.time()
                    self.state = 'Canceled'
                    self.status = CANCELED
                    self.notify()

                if msg == TASK_DONE:
                    self.done()

                if msg == TASK_UPDATE:
                    self.label = 'Sleep: {} {:.2}'.format(*data)
                    self.notify()

                if msg == TASK_ERROR:
                    print('Error ---------')
                    exc_info = data
                    self.set_error(exc_info)

            self.queue_to_main.task_done()


class Test(bpy.types.Operator):
    bl_idname = 'export_scene.previz_test'
    bl_label = 'Refresh Previz projects'

    def execute(self, context):
        self.report({'INFO'}, 'Previz: progress.Test')
        task = DebugAsyncTask()
        tasks_runner.add_task(task)
        return {'FINISHED'}


class CancelTask(bpy.types.Operator):
    bl_idname = 'export_scene.previz_cancel_task'
    bl_label = 'Cancel Previz task'

    task_id = IntProperty(
        name = 'Task ID',
        default = -1
    )

    def execute(self, context):
        self.report({'INFO'}, 'Previz: Cancel task {}'.format(self.task_id))
        tasks_runner.tasks[self.task_id].cancel()
        return {'FINISHED'}


class RaiseException(bpy.types.Operator):
    bl_idname = 'export_scene.previz_raise_exception'
    bl_label = 'Raise Previz exception'


    def execute(self, context):
        sadcsadc
        return {'FINISHED'}


def task2debuginfo(task):
    type, exception, tb = task.error
    d = datetime.datetime.now()
    d_utc = datetime.datetime.utcfromtimestamp(d.timestamp())
    ret = [
        '---- PREVIZ DEBUG INFO START',
        'Date:    : {}'.format(d.isoformat()),
        'Date UTC : {}'.format(d_utc.isoformat()),
        'User     : {}'.format(getpass.getuser()),
        'Blender  : {}'.format(bpy.app.version_string),
        'OS       : {}'.format(platform.platform()),
        'Python   : {}'.format(sys.version),
        'Addon    : {}'.format(''),
        'Version  : {}'.format(''),
        'Task     : {}'.format(task.label),
        'Status   : {}'.format(task.status),
        'Progress : {}'.format(task.progress),
        'Exception: {}'.format(exception.__class__.__name__),
        'Error    : {}'.format(str(exception)),
        'Traceback:',
        ''
    ]
    ret = '\n'.join(ret) + '\n'
    ret += ''.join(traceback.format_tb(tb))
    ret += '\n---- PREVIZ DEBUG INFO END\n'
    return ret


def task2report(task):
    type, exception, tb = task.error

    return '''Previz task error
Task: {}
Exception: {}
Value: {}

See the console for debug information.

The debug information has been copied to the clipboard.
Please paste it to Previz support.
'''.format(task.label, exception.__class__.__name__, exception)


class ShowTaskError(bpy.types.Operator):
    bl_idname = 'export_scene.previz_show_task_error'
    bl_label = 'Show Previz task error'

    task_id = IntProperty(
        name = 'Task ID',
        default = -1
    )

    def execute(self, context):
        task = tasks_runner.tasks[self.task_id]
        self.report({'ERROR'}, task2report(task))
        debug_info = task2debuginfo(task)
        pyperclip.copy(debug_info)
        print(debug_info)
        return {'FINISHED'}


class RemoveTask(bpy.types.Operator):
    bl_idname = 'export_scene.previz_remove_task'
    bl_label = 'Remove Previz task'

    task_id = IntProperty(
        name = 'Task ID',
        default = -1
    )

    def execute(self, context):
        self.report({'INFO'}, 'Previz: Remove task {}'.format(self.task_id))
        tasks_runner.remove_task(self.task_id)
        return {'FINISHED'}


class ManageQueue(bpy.types.Operator):
    bl_idname = 'export_scene.previz_manage_queue'
    bl_label = 'Manage Previz task queue'

    process_polling_interval = 1 # Needs to be a debug User Preferences flag

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.timer = None

    def execute(self, context):
        #print('ManageQueue.execute')
        if tasks_runner.is_empty:
            self.cleanup(context)
            #print('ManageQueue.execute FINISHED')
            return {'FINISHED'}
        self.register_timer(context)
        context.window_manager.modal_handler_add(self)
        #tasks_runner.tick()
        #print('ManageQueue.execute RUNNING_MODAL')
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        self.cleanup(context)

    def modal(self, context, event):
        if event.type == 'ESC':
            #print('ManageQueue.modal CANCELED')
            return {'CANCELED'}

        if event.type == 'TIMER':
            return self.handle_timer_event(context, event)

        return {'PASS_THROUGH'}

    def handle_timer_event(self, context, event):
        if tasks_runner.is_empty:
            self.cleanup(context)
            #print('ManageQueue.handle_timer_event FINISHED')
            return {'FINISHED'}
        tasks_runner.tick()
        #print('ManageQueue.handle_timer_event RUNNING_MODAL')
        return {'RUNNING_MODAL'}

    def cleanup(self, context):
        tasks_runner.cancel()
        self.unregister_timer(context)

    def register_timer(self, context):
        if self.timer is None:
            #print('ManageQueue.register_timer')
            self.timer = context.window_manager.event_timer_add(self.process_polling_interval, context.window)

    def unregister_timer(self, context):
        if self.timer is not None:
            #print('ManageQueue.unregister_timer')
            context.window_manager.event_timer_remove(self.timer)
            self.timer = None


class Panel(bpy.types.Panel):
    bl_label = "PrevizProgress"
    bl_idname = "SCENE_PT_previz_test"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "scene"

    def draw(self, context):
        self.layout.operator(
            'export_scene.previz_test',
            text='Progress test'
        )

        self.layout.operator(
            'export_scene.previz_raise_exception'
        )

        for id, task in tasks_runner.tasks.items():
            row = self.layout.row()
            row.label('{} ({})'.format(task.label, task.state))

            if task.status == ERROR:
                row.operator(
                    'export_scene.previz_show_task_error',
                    text='',
                    icon='ERROR').task_id = id

            if task.is_cancelable and not task.is_finished:
                row.operator(
                    'export_scene.previz_cancel_task',
                    text='',
                    icon='CANCEL').task_id = id

            if task.is_finished:
                row.operator(
                    'export_scene.previz_remove_task',
                    text='',
                    icon='X').task_id = id

            row.enabled = task.status != CANCELING


def register():
    bpy.utils.register_class(Test)
    bpy.utils.register_class(CancelTask)
    bpy.utils.register_class(RemoveTask)
    bpy.utils.register_class(Panel)
    bpy.utils.register_class(ManageQueue)
    bpy.utils.register_class(ShowTaskError)
    bpy.utils.register_class(RaiseException)

    global tasks_runner
    tasks_runner = TasksRunner()

    def refresh_panel(*args, **kwarsg):
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
    tasks_runner.on_task_changed.append(refresh_panel)


def unregister():
    bpy.utils.unregister_class(Test)
    bpy.utils.unregister_class(CancelTask)
    bpy.utils.unregister_class(RemoveTask)
    bpy.utils.unregister_class(Panel)
    bpy.utils.unregister_class(ManageQueue)
    bpy.utils.unregister_class(ShowTaskError)
    bpy.utils.unregister_class(RaiseException)
