#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'


from datetime import datetime
from time import strftime, time

import endpoints
from protorpc import messages
from protorpc import message_types
from protorpc import remote

from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb

from models import ConflictException
from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import StringMessage
from models import BooleanMessage
from models import Conference
from models import ConferenceForm
from models import ConferenceForms
from models import ConferenceQueryForm
from models import ConferenceQueryForms
from models import Session
from models import SessionForm
from models import SessionForms
from models import TeeShirtSize

from settings import WEB_CLIENT_ID
from settings import ANDROID_CLIENT_ID
from settings import IOS_CLIENT_ID
from settings import ANDROID_AUDIENCE

from utils import getUserId

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID
MEMCACHE_ANNOUNCEMENTS_KEY = "RECENT_ANNOUNCEMENTS"
SPEAKER_INFORMATION_KEY = "GET_FEATURED_SPEAKER"
ANNOUNCEMENT_TPL = ('Last chance to attend! The following conferences '
                    'are nearly sold out: %s')

# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": [ "Default", "Topic" ],
}

SESSDEFAULTS = {
    "typeOfSession":["Default", "Type"],
    "maxRegistered": 0,
    "spotsAvailable": 0,
}

OPERATORS = {
            'EQ':   '=',
            'GT':   '>',
            'GTEQ': '>=',
            'LT':   '<',
            'LTEQ': '<=',
            'NE':   '!='
            }

FIELDS =    {
            'CITY': 'city',
            'TOPIC': 'topics',
            'MONTH': 'month',
            'MAX_ATTENDEES': 'maxAttendees',
            }

CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

CONF_GET_TYPE_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
    typeOfSession=messages.StringField(2),
)

GET_SPEAKER_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    speakerLast=messages.StringField(1),
    speakerFirst=messages.StringField(2),
)

GET_DATE_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    sessionDate=messages.StringField(1),
)

CONF_POST_REQUEST = endpoints.ResourceContainer(
    ConferenceForm,
    websafeConferenceKey=messages.StringField(1),
)

SESS_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeSessionKey=messages.StringField(1),
)

SESS_POST_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    websafeSessionKey=messages.StringField(1),
)
# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -


@endpoints.api(name='conference', version='v1', audiences=[ANDROID_AUDIENCE],
    allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID, ANDROID_CLIENT_ID, IOS_CLIENT_ID],
    scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""

# - - - Conference objects - - - - - - - - - - - - - - - - -

    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf


    def _createConferenceObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Conference 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # add default values for those missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month based on start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()
            data['month'] = data['startDate'].month
        else:
            data['month'] = 0
        if data['endDate']:
            data['endDate'] = datetime.strptime(data['endDate'][:10], "%Y-%m-%d").date()

        # set seatsAvailable to be same as maxAttendees on creation
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]
        # generate Profile Key based on user ID and Conference
        # ID based on Profile key get Conference key from ID
        p_key = ndb.Key(Profile, user_id)
        c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
        c_key = ndb.Key(Conference, c_id, parent=p_key)
        data['key'] = c_key
        data['organizerUserId'] = request.organizerUserId = user_id

        # create Conference, send email to organizer confirming
        # creation of Conference & return (modified) ConferenceForm
        Conference(**data).put()
        taskqueue.add(params={'email': user.email(),
            'conferenceInfo': repr(request)},
            url='/tasks/send_confirmation_email'
        )
        return request


    @ndb.transactional()
    def _updateConferenceObject(self, request):
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}

        # update existing conference
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        # check that user is owner
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the owner can update the conference.')

        # Not getting all the fields, so don't create a new object; just
        # copy relevant fields from ConferenceForm to Conference object
        for field in request.all_fields():
            data = getattr(request, field.name)
            # only copy fields where we get data
            if data not in (None, []):
                # special handling for dates (convert string to Date)
                if field.name in ('startDate', 'endDate'):
                    data = datetime.strptime(data, "%Y-%m-%d").date()
                    if field.name == 'startDate':
                        conf.month = data.month
                # write to Conference object
                setattr(conf, field.name, data)
        conf.put()
        prof = ndb.Key(Profile, user_id).get()
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(ConferenceForm, ConferenceForm, path='conference',
            http_method='POST', name='createConference')
    def createConference(self, request):
        """Create new conference."""
        return self._createConferenceObject(request)


    @endpoints.method(CONF_POST_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='PUT', name='updateConference')
    def updateConference(self, request):
        """Update conference w/provided fields & return w/updated info."""
        return self._updateConferenceObject(request)


    @endpoints.method(CONF_GET_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='GET', name='getConference')
    def getConference(self, request):
        """Return requested conference (by websafeConferenceKey)."""
        # get Conference object from request; bail if not found
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        prof = conf.key.parent().get()
        # return ConferenceForm
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='getConferencesCreated',
            http_method='POST', name='getConferencesCreated')
    def getConferencesCreated(self, request):
        """Return conferences created by user."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # create ancestor query for all key matches for this user
        confs = Conference.query(ancestor=ndb.Key(Profile, user_id))
        prof = ndb.Key(Profile, user_id).get()
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, getattr(prof, 'displayName')) for conf in confs]
        )


    def _getQuery(self, request):
        """Return formatted query from the submitted filters."""
        q = Conference.query()
        inequality_filter, filters = self._formatFilters(request.filters)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Conference.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Conference.name)

        for filtr in filters:
            if filtr["field"] in ["month", "maxAttendees"]:
                filtr["value"] = int(filtr["value"])
            formatted_query = ndb.query.FilterNode(filtr["field"], filtr["operator"], filtr["value"])
            q = q.filter(formatted_query)
        return q


    def _formatFilters(self, filters):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None

        for f in filters:
            filtr = {field.name: getattr(f, field.name) for field in f.all_fields()}

            try:
                filtr["field"] = FIELDS[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
                raise endpoints.BadRequestException("Filter contains invalid field or operator.")

            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation has been used in previous filters
                # disallow the filter if inequality was performed on a different field before
                # track the field on which the inequality operation is performed
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException("Inequality filter is allowed on only one field.")
                else:
                    inequality_field = filtr["field"]

            formatted_filters.append(filtr)
        return (inequality_field, formatted_filters)


    @endpoints.method(ConferenceQueryForms, ConferenceForms,
            path='queryConferences',
            http_method='POST',
            name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        conferences = self._getQuery(request)

        # need to fetch organiser displayName from profiles
        # get all keys and use get_multi for speed
        organisers = [(ndb.Key(Profile, conf.organizerUserId)) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return individual ConferenceForm object per Conference
        return ConferenceForms(
                items=[self._copyConferenceToForm(conf, names[conf.organizerUserId]) for conf in \
                conferences]
        )


# - - - Session objects - - - - - - - - - - - - - - - - -


    def _copySessionToForm(self, sess, websafeCK):
        """Copy relevant fields from Session to SessionForm."""
        ss = SessionForm()
        for field in ss.all_fields():
            if hasattr(sess, field.name):
                # convert Date to date string; 
                # convert Time to 24 hr string;
                # copy all others
                if field.name.endswith('Date'):
                    setattr(ss, field.name, str(getattr(sess, field.name)))
                elif field.name.endswith('Time'):
                    setattr(ss, field.name, str(getattr(sess, field.name)))
                else:
                    setattr(ss, field.name, getattr(sess, field.name))
            elif field.name == "websafeSessionKey":
                setattr(ss, field.name, sess.key.urlsafe())
        if websafeCK:
            setattr(ss, 'websafeCK', websafeCK)
        ss.check_initialized()
        return ss


    def _createSessionObject(self, request):
        """Create or update Session object, returning SessionForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        #need both conference name and session name to add a session
        if not request.sessionName:
            raise endpoints.BadRequestException("Session 'name' field required")
        if not request.websafeCK:
            raise endpoints.BadRequestException("Conference 'web safe key' field required")

        # copy SessionForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeSessionKey']
        del data['websafeCK']
        
        # add default values for those missing (both data model & outbound 
        # Message)
        for df in SESSDEFAULTS:
            if data[df] in (None, []):
                data[df] = SESSDEFAULTS[df]
                setattr(request, df, SESSDEFAULTS[df])

        # convert dates from strings to Date objects

        if data['sessionDate']:
            data['sessionDate'] = datetime.strptime(data['sessionDate'][:10], "%Y-%m-%d").date()
        if data['startTime']:
            data['startTime'] = datetime.strptime(data['startTime'][:5], "%H:%M").time()

        # set spotsAvailable to be same as maxRegistered on creation
        if data["maxRegistered"] > 0:
            data["spotsAvailable"] = data["maxRegistered"]

        # check if conf exists given conferenceName
        # get conference; check that it exists
        conf = ndb.Key(urlsafe=request.websafeCK).get()

        # only the creator of a conference can populate it with sessions
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the owner of the conference can update this session.')

        if not conf:
           raise endpoints.NotFoundException(
               'No conference found with key: %s' % request.websafeCK)

        # create new session key
        s_id = Session.allocate_ids(size=1, parent=conf.key)[0]
        s_key = ndb.Key(Session, s_id, parent=conf.key)
        data['key'] = s_key

        # create Session & return (modified) SessionForm
        Session(**data).put()

        # add task queue for featuredSpeaker after creating session
        #if data['speakerLast'] and data['speakerFirst']:
        
        taskqueue.add(
                params={'websafeCK': request.websafeCK,
                'speakerFirst': data['speakerFirst'],
                'speakerLast': data['speakerLast']},
                url='/tasks/get_featured_speaker'
            )

        return request



    @endpoints.method(CONF_GET_REQUEST, SessionForms,
            path='getConferenceSessions/{websafeConferenceKey}',
            http_method='GET', name='getConferenceSessions')
    def getConferenceSessions(self, request):
        """Return sessions of a conference."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # create ancestor query for all key matches for this conference
        wsk = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsk).get()

        sessions = Session.query(ancestor=ndb.Key(urlsafe=wsk))
        if not sessions:
            raise endpoints.NotFoundException(
                'No sessions found for conference with key: %s' % wsk)
        # return set of SessionForm objects per Session
        return SessionForms(
            items=[self._copySessionToForm(sess, wsk) for sess in sessions]
        )


    @endpoints.method(CONF_GET_TYPE_REQUEST, SessionForms,
            path='getConferenceSessions/{websafeConferenceKey}/{typeOfSession}',
            http_method='GET', name='getConferenceSessionsByType')
    def getConferenceSessionsByType(self, request):
        """Return sessions of a conference of a particular type."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # create ancestor query for all key matches for this conference
        wsk = request.websafeConferenceKey
        sessionType = request.typeOfSession

        conf = ndb.Key(urlsafe=wsk).get()

        # query all sessions in this conference of a particular type
        sessions = Session.query(ancestor=ndb.Key(urlsafe=wsk))
        sessions = sessions.filter(Session.typeOfSession==sessionType)
        
        if not sessions:
            raise endpoints.NotFoundException(
                'No sessions found for conference with key: %s and type: %s' % wsk % sessionType)
        # return set of SessionForm objects per Session
        return SessionForms(
            items=[self._copySessionToForm(sess, wsk) for sess in sessions]
        )


    @endpoints.method(GET_SPEAKER_REQUEST, SessionForms,
            path='getSessions/{speakerLast}/{speakerFirst}',
            http_method='GET', name='getSessionsBySpeaker')
    def getSessionsBySpeaker(self, request):
        """Return sessions having the same speaker across conferences."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # create ancestor query for all key matches for this conference
        first = request.speakerFirst
        last = request.speakerLast
        fullname = str(first)+" "+str(last)

        # query all sessions by speaker names
        sessions = Session.query()
        sessions = sessions.filter(Session.speakerLast==last)
        sessions = sessions.filter(Session.speakerFirst==first)

        if not sessions:
            raise endpoints.NotFoundException(
                'No sessions found for speaker with name: %s' % fullname)

        # return set of SessionForm objects per Session
        return SessionForms(
            items=[self._copySessionToForm(sess, "") for sess in sessions]
        )


    @endpoints.method(SessionForm, SessionForm,
            path='createNewSession',
            http_method='POST', name='createSession')
    def createSession(self, request):
        """Create new session."""
        return self._createSessionObject(request)


    @endpoints.method(SESS_GET_REQUEST, SessionForm,
            path='session/{websafeSessionKey}',
            http_method='GET', name='getSession')
    def getSession(self, request):
        """Return requested session (by websafeSessionKey)."""
        # get Session object from request; bail if not found
        sess = ndb.Key(urlsafe=request.websafeSessionKey).get()
        if not sess:
            raise endpoints.NotFoundException(
                'No session found with key: %s' % request.websafeSessionKey)
        conf = sess.key.parent().get()
        # return SessionForm
        return self._copySessionToForm(sess, "")


# - - - Session Filters - - - - - - - - - - - - - - - - - - 


    @endpoints.method(GET_DATE_REQUEST, SessionForms,
            path='getSessions/{sessionDate}',
            http_method='GET', name='getSessionsByDate')
    def getSessionsByDate(self, request):
        """Return sessions having a particular date."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # create date format
        if not request.sessionDate:
            raise endpoints.UnauthorizedException('A date value is required')
        else: 
            sessionDate = datetime.strptime(request.sessionDate[:10], "%Y-%m-%d").date()

        # query by date
        sessions = Session.query()
        sessions = sessions.filter(Session.sessionDate==sessionDate)

        if not sessions:
            raise endpoints.NotFoundException(
                'No sessions found for this date.')

        # return set of SessionForm objects per Session
        return SessionForms(
            items=[self._copySessionToForm(sess, "") for sess in sessions]
        )


    @endpoints.method(CONF_GET_REQUEST, SessionForms,
            path='getAvailableSessions/{websafeConferenceKey}',
            http_method='GET', name='getAvailableSessions')
    def getAvailableSessions(self, request):
        """Return sessions having spots available in a conference."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # create ancestor query for all key matches for this conference
        wsk = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsk).get()

        sessions = Session.query(ancestor=ndb.Key(urlsafe=wsk))

        if not sessions:
            raise endpoints.NotFoundException(
                'No sessions found for this conference.')

        # check if available sessions exist

        # query by date
        sessions = sessions.filter(Session.spotsAvailable > 0)

        if not sessions:
            raise endpoints.NotFoundException(
                'No available sessions are found for this conference.')

        # return set of SessionForm objects per Session
        return SessionForms(
            items=[self._copySessionToForm(sess, "") for sess in sessions]
        )




# - - - Session Wishlist - - - - - - - - - - - - - - - - - - 


    @ndb.transactional()
    def _sessionWishlist(self, request, add=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if sess exists given websafeSessionKey
        wssk = request.websafeSessionKey
        sess = ndb.Key(urlsafe=wssk).get()
        if not sess:
            raise endpoints.NotFoundException(
                'No session found with key: %s' % wssk)

        # add session
        if add:
            # check if session already added, otherwise add
            if wssk in prof.sessionKeysAdded:
                raise ConflictException(
                    "You have already added this session")

            # add session
            prof.sessionKeysAdded.append(wssk)
            retval = True

        # remove session
        else:
            # check if session already added
            if wssk in prof.sessionKeysAdded:

                # remove session
                prof.sessionKeysAdded.remove(wssk)
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        return BooleanMessage(data=retval)


    @endpoints.method(message_types.VoidMessage, SessionForms,
            path='sessions/wishlist',
            http_method='GET', name='getSessionsInWishlist')
    def getSessionsInWishlist(self, request):
        """Get list of sessions that user has added to their wishlist."""
        # get user Profile
        prof = self._getProfileFromUser() 

        # get sessions from user Profile
        sess_keys = [ndb.Key(urlsafe=wssk) for wssk in prof.sessionKeysAdded]
        sessions = ndb.get_multi(sess_keys)

        # return set of SessionForm objects per Session
        return SessionForms(items=[self._copySessionToForm(sess, "")\
         for sess in sessions]
        )



    @endpoints.method(SESS_GET_REQUEST, BooleanMessage,
            path='wishlist/{websafeSessionKey}',
            http_method='POST', name='addSessionToWishlist')
    def addSessionToWishlist(self, request):
        """Add the selected session to user's wishlist."""
        return self._sessionWishlist(request)


    @endpoints.method(SESS_GET_REQUEST, BooleanMessage,
            path='wishlist/{websafeSessionKey}',
            http_method='DELETE', name='deleteSessionInWishlist')
    def deleteSessionInWishlist(self, request):
        """Remove the selected session from user's wishlist."""
        return self._sessionWishlist(request, add=False)



# - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                    setattr(pf, field.name, getattr(TeeShirtSize, getattr(prof, field.name)))
                else:
                    setattr(pf, field.name, getattr(prof, field.name))
        pf.check_initialized()
        return pf


    def _getProfileFromUser(self):
        """Return user Profile from datastore, creating new one if non-existent."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # get Profile from datastore
        user_id = getUserId(user)
        p_key = ndb.Key(Profile, user_id)
        profile = p_key.get()
        # create new Profile if not there
        if not profile:
            profile = Profile(
                key = p_key,
                displayName = user.nickname(),
                mainEmail= user.email(),
                teeShirtSize = str(TeeShirtSize.NOT_SPECIFIED),
            )
            profile.put()

        return profile      # return Profile


    def _doProfile(self, save_request=None):
        """Get user Profile and return to user, possibly updating it first."""
        # get user Profile
        prof = self._getProfileFromUser()

        # if saveProfile(), process user-modifyable fields
        if save_request:
            for field in ('displayName', 'teeShirtSize'):
                if hasattr(save_request, field):
                    val = getattr(save_request, field)
                    if val:
                        setattr(prof, field, str(val))
                        #if field == 'teeShirtSize':
                        #    setattr(prof, field, str(val).upper())
                        #else:
                        #    setattr(prof, field, val)
                        prof.put()

        # return ProfileForm
        return self._copyProfileToForm(prof)


    @endpoints.method(message_types.VoidMessage, ProfileForm,
            path='profile', http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()


    @endpoints.method(ProfileMiniForm, ProfileForm,
            path='profile', http_method='POST', name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""
        return self._doProfile(request)


# - - - Announcements - - - - - - - - - - - - - - - - - - - -

    @staticmethod
    def _cacheAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & putAnnouncement().
        """
        confs = Conference.query(ndb.AND(
            Conference.seatsAvailable <= 5,
            Conference.seatsAvailable > 0)
        ).fetch(projection=[Conference.name])

        if confs:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            announcement = ANNOUNCEMENT_TPL % (
                ', '.join(conf.name for conf in confs))
            memcache.set(MEMCACHE_ANNOUNCEMENTS_KEY, announcement)
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete(MEMCACHE_ANNOUNCEMENTS_KEY)

        return announcement


    @endpoints.method(message_types.VoidMessage, StringMessage,
            path='conference/announcement/get',
            http_method='GET', name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""
        return StringMessage(data=memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY) or "")


    @endpoints.method(message_types.VoidMessage, StringMessage,
            path='conference/announcement/put',
            http_method='PUT', name='putAnnouncement')
    def putAnnouncement(self, request):
        """Put Announcement into memcache"""
        return StringMessage(data=self._cacheAnnouncement())


# - - - New Speaker Information - - - - - - - - - - - - - -

    # def _cacheSpeakerInformation(self):
    #     """Create information & assign to memcache.
    #     """

    #     # obtain names of sessions having this speaker
    #     #q = Session.query(ancestor=ndb.Key(urlsafe=websafeCK))
    #     #q = q.query(Session.speakerFirst == speakerFirst)
    #     #q = q.query(Session.speakerLast == speakerLast)

    #     #sessions = q.fetch()
    #     # determine if speaker also at other sessions
    #     #result = sessions.count()
    #     result = 2

    #     if (result > 1):
    #         # If there are other sessions with this speaker,
    #         # format the information and set it in memcache
    #         speakerInformation = "THIS IS THE SPEAKER INFORMATION"
    #         #speakerInformation = INFORMATION_TPL\
    #         #    % (', '.join(session.sessionName for session in sessions))
    #         memcache.set(SPEAKER_INFORMATION_KEY, speakerInformation)
    #     else:
    #          # If there are no other speaker hosted sessions,
    #          # delete the memcache information entry
    #         speakerInformation = ""
    #         memcache.delete(SPEAKER_INFORMATION_KEY)

    #     return speakerInformation


    @endpoints.method(message_types.VoidMessage, StringMessage,
            path='speaker/information/get',
            http_method='GET', name='getFeaturedSpeaker')
    def getFeaturedSpeaker(self, request):
        """Return speaker information from memcache."""
        return StringMessage(data=memcache.get(SPEAKER_INFORMATION_KEY) or "")



# - - - Registration - - - - - - - - - - - - - - - - - - - -

    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # register
        if reg:
            # check if user already registered otherwise add
            if wsck in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(wsck)
            conf.seatsAvailable -= 1
            retval = True

        # unregister
        else:
            # check if user already registered
            if wsck in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(wsck)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='conferences/attending',
            http_method='GET', name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""
        prof = self._getProfileFromUser() # get user Profile
        conf_keys = [ndb.Key(urlsafe=wsck) for wsck in prof.conferenceKeysToAttend]
        conferences = ndb.get_multi(conf_keys)

        # get organizers
        organisers = [ndb.Key(Profile, conf.organizerUserId) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return set of ConferenceForm objects per Conference
        return ConferenceForms(items=[self._copyConferenceToForm(conf, names[conf.organizerUserId])\
         for conf in conferences]
        )


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='POST', name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='DELETE', name='unregisterFromConference')
    def unregisterFromConference(self, request):
        """Unregister user for selected conference."""
        return self._conferenceRegistration(request, reg=False)


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='filterPlayground',
            http_method='GET', name='filterPlayground')
    def filterPlayground(self, request):
        """Filter Playground"""
        q = Conference.query()
        # field = "city"
        # operator = "="
        # value = "London"
        # f = ndb.query.FilterNode(field, operator, value)
        # q = q.filter(f)
        q = q.filter(Conference.city=="London")
        q = q.filter(Conference.topics=="Medical Innovations")
        q = q.filter(Conference.month==6)

        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") for conf in q]
        )


api = endpoints.api_server([ConferenceApi]) # register API
