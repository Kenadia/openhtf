# Copyright 2015 Google Inc. All Rights Reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""User input module for OpenHTF.

Allows tests to prompt for user input using the framework, so prompts can be
presented via the CLI interface, the included web frontend, and custom
frontends alike. Any other part of the framework that needs to access shared
prompt state should use the openhtf.prompts pseudomodule.
"""

import collections
import functools
import logging
import os
import platform
import select
import sys
import threading
import time
import uuid

from openhtf import PhaseOptions
from openhtf import plugs
from openhtf.util import argv


if platform.system() != 'Windows':
  import termios

_LOG = logging.getLogger(__name__)


class PromptInputError(Exception):
  """Raised in the event that a prompt returns without setting the response."""


class MultiplePromptsError(Exception):
  """Raised if a prompt is invoked while there is an existing prompt."""


class PromptUnansweredError(Exception):
  """Raised when a prompt times out or otherwise comes back unanswered."""


class WrongPromptError(Exception):
  """Raise when a response is made to an unknown prompt ID."""


Prompt = collections.namedtuple('Prompt', 'id message text_input options')


class ConsolePrompt(threading.Thread):
  """Thread that displays a prompt to the console and waits for a response."""

  def __init__(self, message, callback):
    super(ConsolePrompt, self).__init__()
    self.daemon = True
    self._message = message
    self._callback = callback
    self._stopped = False
    self._answered = False

  def Stop(self):
    """Mark this ConsolePrompt as stopped."""
    self._stopped = True
    if not self._answered:
      print "Nevermind; prompt was answered from elsewhere."

  def run(self):
    """Main logic for this thread to execute."""
    try:
      if platform.system() == 'Windows':
        # Windows doesn't support file-like objects for select(), so fall back
        # to raw_input().
        accepted = False
        while not (self._stopped or accepted):
          response = raw_input(self._message + '\n\r')
          accepted = self._callback(response)
        if accepted:
          self._answered = True
      else:
        # First, display the prompt to the console.
        print self._message

        # Before reading, clear any lingering buffered terminal input.
        if sys.stdin.isatty():
          termios.tcflush(sys.stdin, termios.TCIFLUSH)

        line = ''
        while not self._stopped:
          inputs, _, _ = select.select([sys.stdin], [], [], 0.001)
          for stream in inputs:
            if stream is sys.stdin:
              new = os.read(sys.stdin.fileno(), 1024)
              if not new:
                # Hit EOF!
                if not sys.stdin.isatty():
                  # We're running in the background somewhere, so the only way
                  # to respond to this prompt is the UI. Let's just wait for
                  # that to happen now. We'll give them a week :)
                  print "Waiting for a non-console response."
                  time.sleep(60*60*24*7)
                else:
                  # They hit ^D (to insert EOF). Tell them to hit ^C if they
                  # want to actually quit.
                  print "Hit ^C (Ctrl+c) to exit."
                  break
              line += new
              if '\n' in line:
                response = line[:line.find('\n')]
                try:
                  accepted = self._callback(response)
                except WrongPromptError:
                  return
                if accepted:
                  self._answered = True
                  return
                print '\nInvalid response.\n\n' + self._message
                line = ''
    finally:
      self._stopped = True


class UserInput(plugs.FrontendAwareBasePlug):
  """Get user input from inside test phases."""

  def __init__(self):
    super(UserInput, self).__init__()
    self._prompt = None
    self._console_prompt = None
    self._response = None
    self._cond = threading.Condition()

  def _asdict(self):
    """Return a dict representation of the current prompt."""
    with self._cond:
      if self._prompt is None:
        return
      return {
          'id': self._prompt.id.hex,
          'message': self._prompt.message,
          'text-input': self._prompt.text_input,
          'options': self._prompt.options,
      }

  def _create_prompt(self, message, text_input, options):
    """Sets the prompt."""
    if options == []:
      raise ValueError('Prompt options cannot be an empty list.')
    if text_input and options:
      raise ValueError('Cannot set options on a prompt with text_input=True.')

    prompt_id = uuid.uuid4()
    _LOG.debug('Displaying prompt (%s): "%s"%s', prompt_id, message,
               ', Expects text' if text_input else '')

    self._response = None
    self._prompt = Prompt(prompt_id, message, text_input, options)

    if options:
      options_string = '\n'.join('%d: %s' % (i + 1, option)
                                 for i, option in enumerate(options))
      help_text = ('(Please respond with an integer from 1 to %d.)' %
                   len(options))
      console_message = '%s\n\n%s\n%s' % (options_string, message, help_text)
    else:
      console_message = message

    self._console_prompt = ConsolePrompt(
        console_message, functools.partial(self.respond, prompt_id))

    self._console_prompt.start()
    self.notify_update()

  def _remove_prompt(self):
    """Ends the prompt."""
    self._prompt = None
    self._console_prompt.Stop()
    self._console_prompt = None
    self.notify_update()

  def prompt(self, message, text_input=False, options=None, timeout_s=None):
    """Prompt and wait for a user response to the given message.

    Three prompt types are supported:
      - No input (text_input=False, options=None)
      - Text input (text_input=True, options=None)
      - Option selection (text_input=False, options=list)

    Args:
      message: The message to display to the user.
      text_input: True iff the user needs to provide a string back.
      options: A list of strings for the user to select from, or None. Should
          not be set when text_input is True. List cannot be empty.
      timeout_s: Seconds to wait before raising a PromptUnansweredError.

    Returns:
      Depending on the prompt type:
        - No input: the empty string
        - Text input: the user's response
        - Option selection: integer index of one of the options

    Raises:
      MultiplePromptsError: There was already an existing prompt.
      PromptUnansweredError: Timed out waiting for the user to respond.
    """
    self.start_prompt(message, text_input, options)
    return self.wait_for_prompt(timeout_s)

  def start_prompt(self, message, text_input=False, options=None):
    """Creates a prompt without blocking on the user's response."""
    with self._cond:
      if self._prompt:
        raise MultiplePromptsError
      self._create_prompt(message, text_input, options)

  def wait_for_prompt(self, timeout_s=None):
    """Waits for and returns the user's response to the last prompt."""
    with self._cond:
      if self._prompt:
        if timeout_s is None:
          self._cond.wait(3600 * 24 * 365)
        else:
          self._cond.wait(timeout_s)
      if self._response is None:
        raise PromptUnansweredError
      return self._response

  def respond(self, prompt_id, response):
    """Respond to the prompt that has the given ID.

    Args:
      prompt_id: A UUID instance or a string representing a UUID.
      response: A string response to the given prompt.

    Returns:
      True if the response was used, otherwise False.

    Raises:
      WrongPromptError: There is no active prompt or the prompt ID being
          responded to doesn't match the active prompt.
    """
    if type(prompt_id) == str:
      prompt_id = uuid.UUID(prompt_id)

    _LOG.debug('Responding to prompt (%s): "%s"', prompt_id.hex, response)

    with self._cond:
      if not self._prompt or self._prompt.id != prompt_id:
        raise WrongPromptError

      # If the prompt has options, validate the response.
      if self._prompt.options:
        try:
          index = int(response) - 1
          if index < 0 or index >= len(self._prompt.options):
            return False
          self._response = index
        except ValueError:
          return False
      else:
        self._response = response

      self._remove_prompt()
      self._cond.notifyAll()
    return True


def prompt_for_test_start(
    message='Enter a DUT ID in order to start the test.', timeout_s=60*60*24,
    validator=lambda sn: sn):
  """Return an OpenHTF phase for use as a prompt-based start trigger.

    Args:
      message: The message to display to the user.
      timeout_s: Seconds to wait before raising a PromptUnansweredError.
      validator: Function used to validate or modify the serial number.
  """

  @PhaseOptions(timeout_s=timeout_s)
  @plugs.plug(prompts=UserInput)
  def trigger_phase(test, prompts):
    """Test start trigger that prompts the user for a DUT ID."""

    dut_id = prompts.prompt(message=message, text_input=True,
                            timeout_s=timeout_s)
    test.test_record.dut_id = validator(dut_id)

  return trigger_phase
