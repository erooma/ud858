#!/usr/bin/env python

"""
main.py -- Udacity conference server-side Python App Engine
    HTTP controller handlers for memcache & task queue access

$Id$

created by wesc on 2014 may 24

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'

import webapp2
from google.appengine.api import app_identity
from google.appengine.api import mail
from google.appengine.ext import ndb
from google.appengine.api import memcache
from google.appengine.api import taskqueue

from models import Session


from conference import ConferenceApi

SPEAKER_INFORMATION_KEY = "GET_FEATURED_SPEAKER"
INFORMATION_TPL = ('Your speaker %s is also leading these other sessions: %s')


class SetAnnouncementHandler(webapp2.RequestHandler):
    def get(self):
        """Set Announcement in Memcache."""
        ConferenceApi._cacheAnnouncement()
        self.response.set_status(204)


class SetSpeakerHandler(webapp2.RequestHandler):
    def post(self):
        """Set speaker infomation in memcache"""

        # obtain names of sessions having this speaker
        q = Session.query(ancestor=ndb.Key(urlsafe=self.request.get('websafeCK')))
        q = q.filter(Session.speakerLast == self.request.get('speakerLast'))
        q = q.filter(Session.speakerFirst == self.request.get('speakerFirst'))

        result=q.count()
        sessions = q.fetch()
        fullName = self.request.get('speakerFirst')+" "+ self.request.get('speakerLast')
        # determine if speaker also at other sessions
        if result and (result > 1):
                # If there are other sessions with this speaker,
                # format the information and set it in memcache
                speakerInformation = INFORMATION_TPL\
                    % (fullName,\
                      (', '.join(session.sessionName for session in sessions)))
                memcache.set(SPEAKER_INFORMATION_KEY, speakerInformation)
        else:
                # If there are no other speaker hosted sessions,
                # delete the memcache information entry
                speakerInformation = ""
                memcache.delete(SPEAKER_INFORMATION_KEY)

        self.response.set_status(204)
        

class SendConfirmationEmailHandler(webapp2.RequestHandler):
    def post(self):
        """Send email confirming Conference creation."""
        mail.send_mail(
            'noreply@%s.appspotmail.com' % (
                app_identity.get_application_id()),     # from
            self.request.get('email'),                  # to
            'You created a new Conference!',            # subj
            'Hi, you have created a following '         # body
            'conference:\r\n\r\n%s' % self.request.get(
                'conferenceInfo')
        )


app = webapp2.WSGIApplication([
    ('/crons/set_announcement', SetAnnouncementHandler),
    ('/tasks/send_confirmation_email', SendConfirmationEmailHandler),
    ('/tasks/get_featured_speaker', SetSpeakerHandler),
], debug=True)
