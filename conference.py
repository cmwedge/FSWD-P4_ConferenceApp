#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'

import calendar
import time
from datetime import datetime

import logging

import endpoints
import operator

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
from models import TeeShirtSize
from models import ConferenceSession
from models import ConferenceSessionForm
from models import ConferenceSessionForms
from models import ConferenceSessionType
from models import ConferenceSessionCreatedResponse
from models import SessionWishlistItem
from models import GetFeaturedSpeakerResponse
from models import GetConferenceSpeakersResponse
from models import ConferenceWithWishlistSession
from models import ConferencesWithWishlistSessionResponse

from settings import WEB_CLIENT_ID
from settings import ANDROID_CLIENT_ID
from settings import IOS_CLIENT_ID
from settings import ANDROID_AUDIENCE

from utils import getUserId

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID
MEMCACHE_ANNOUNCEMENTS_KEY = "RECENT_ANNOUNCEMENTS"
ANNOUNCEMENT_TPL = ('Last chance to attend! The following conferences '
                    'are nearly sold out: %s')
# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": ["Default", "Topic"],
}

OPERATORS = {
    'EQ': '=',
    'GT': '>',
    'GTEQ': '>=',
    'LT': '<',
    'LTEQ': '<=',
    'NE': '!='
}

FIELDS = {
    'CITY': 'city',
    'TOPIC': 'topics',
    'MONTH': 'month',
    'MAX_ATTENDEES': 'maxAttendees',
}

CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

CONF_POST_REQUEST = endpoints.ResourceContainer(
    ConferenceForm,
    websafeConferenceKey=messages.StringField(1)
)

CREATE_CSESSION_REQ = endpoints.ResourceContainer(
    name=messages.StringField(1, required=True),
    highlights=messages.StringField(2),
    speaker=messages.StringField(3, required=True),
    duration=messages.IntegerField(4, required=True),
    typeOfSession=messages.EnumField(ConferenceSessionType, 5, required=True),
    date=messages.StringField(6, required=True),
    startTime=messages.StringField(7, required=True),
    conferenceKey=messages.StringField(8, required=True),
)

GET_CSESSION_BY_CID_REQ = endpoints.ResourceContainer(
    conferenceKey=messages.StringField(1, required=True)
)

GET_CSESSION_BY_TYPE_REQ = endpoints.ResourceContainer(
    conferenceKey=messages.StringField(1, required=True),
    typeOfSession=messages.EnumField(ConferenceSessionType, 2, required=True)
)

GET_CSESSION_BY_SPEAKER_REQ = endpoints.ResourceContainer(
    speaker=messages.StringField(1, required=True),
)

CREATE_WISHLIST_ITEM_REQ = endpoints.ResourceContainer(
    sessionKey=messages.StringField(1, required=True)
)

GET_FEATURED_SPEAKER_REQ = endpoints.ResourceContainer(
    conferenceKey=messages.StringField(1, required=True)
)

GET_CONF_SPEAKERS_REQ = endpoints.ResourceContainer(
    conferenceKey=messages.StringField(1, required=True)
)

# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -


@endpoints.api(
    name='conference',
    version='v1',
    audiences=[ANDROID_AUDIENCE],
    allowed_client_ids=[
        WEB_CLIENT_ID,
        API_EXPLORER_CLIENT_ID,
        ANDROID_CLIENT_ID,
        IOS_CLIENT_ID],
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
            raise endpoints.BadRequestException(
                "Conference 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {
            field.name: getattr(
                request,
                field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # add default values for those missing (both data model & outbound
        # Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month based on
        # start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(
                data['startDate'][
                    :10],
                "%Y-%m-%d").date()
            data['month'] = data['startDate'].month
        else:
            data['month'] = 0
        if data['endDate']:
            data['endDate'] = datetime.strptime(
                data['endDate'][
                    :10],
                "%Y-%m-%d").date()

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
        data = {
            field.name: getattr(
                request,
                field.name) for field in request.all_fields()}

        # update existing conference
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' %
                request.websafeConferenceKey)

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

    @endpoints.method(
        message_types.VoidMessage,
        ConferencesWithWishlistSessionResponse,
        path='getConferencesWithWishlistedSessions',
        http_method='GET',
        name='getConferencesWithWishlistedSessions')
    def getConferencesWithWishlistedSessions(self, request):
        """Returns conferences which have sessions the user has wishlisted."""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        session_keys = [
            wl.sessionKey for wl in SessionWishlistItem.query(
                SessionWishlistItem.userId == user_id)]

        conf_keys = [ndb.Key(urlsafe=k).parent().urlsafe() for k in session_keys]

        conf_counts = {}
        for ck in conf_keys:
            if ck in conf_counts:
                conf_counts[ck] += 1
            else:
                conf_counts[ck] = 1

        confs = []
        for (k,v) in sorted(conf_counts.items(), key=operator.itemgetter(1)):
            confs.append(ConferenceWithWishlistSession(
                conference=self._copyConferenceToForm(
                    ndb.Key(urlsafe=k).get(), None),
                wishlistedSessions=v))

        return ConferencesWithWishlistSessionResponse(
            conferences=list(reversed(confs))
        )

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
                'No conference found with key: %s' %
                request.websafeConferenceKey)
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
            items=[
                self._copyConferenceToForm(
                    conf,
                    getattr(
                        prof,
                        'displayName')) for conf in confs])

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
            formatted_query = ndb.query.FilterNode(
                filtr["field"],
                filtr["operator"],
                filtr["value"])
            q = q.filter(formatted_query)
        return q

    def _formatFilters(self, filters):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None

        for f in filters:
            filtr = {
                field.name: getattr(
                    f,
                    field.name) for field in f.all_fields()}

            try:
                filtr["field"] = FIELDS[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
                raise endpoints.BadRequestException(
                    "Filter contains invalid field or operator.")

            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation has been used in previous filters
                # disallow the filter if inequality was performed on a different field before
                # track the field on which the inequality operation is
                # performed
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException(
                        "Inequality filter is allowed on only one field.")
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
        organisers = [(ndb.Key(Profile, conf.organizerUserId))
                      for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return individual ConferenceForm object per Conference
        return ConferenceForms(
            items=[
                self._copyConferenceToForm(
                    conf,
                    names[
                        conf.organizerUserId]) for conf in conferences])


# - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                    setattr(
                        pf,
                        field.name,
                        getattr(
                            TeeShirtSize,
                            getattr(
                                prof,
                                field.name)))
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
                key=p_key,
                displayName=user.nickname(),
                mainEmail=user.email(),
                teeShirtSize=str(TeeShirtSize.NOT_SPECIFIED),
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
                        # if field == 'teeShirtSize':
                        #    setattr(prof, field, str(val).upper())
                        # else:
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
        return StringMessage(
            data=memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY) or "")

# - - - Speakers - - - - - - - - - - - - - - - - - - - -

    @staticmethod
    def _updateFeaturedSpeaker(speaker, conferenceKey):
        """Updates the featured speaker in memcache."""
        if ndb.Key(urlsafe=request.conferenceKey).kind() != "Conference":
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.conferenceKey)

        q = ConferenceSession.query(ancestor == ndb.Key(
                                        urlsafe=conferenceKey))

        q = q.filter(ConferenceSession.speaker == speaker)

        if q.count() > 1:
            fskey = 'featuredSpeaker-' + conferenceKey
            memcache.set(fskey, (speaker, [qi.name for qi in q]))

    @endpoints.method(GET_CONF_SPEAKERS_REQ, GetConferenceSpeakersResponse,
                      path='getConferenceSpeakers',
                      http_method='GET', name='getConferenceSpeakers')
    def getConferenceSpeakers(self, request):
        """Gets all speakers at the desired conference."""
        if ndb.Key(urlsafe=request.conferenceKey).kind() != "Conference":
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.conferenceKey)

        q = ConferenceSession.query(
            ancestor=ndb.Key(
                urlsafe=request.conferenceKey))
        
        q = q.fetch(projection=[ConferenceSession.speaker])

        return GetConferenceSpeakersResponse(
            speakers=list({s.speaker for s in q})
        )

    @endpoints.method(GET_FEATURED_SPEAKER_REQ, GetFeaturedSpeakerResponse,
                      path='getFeaturedSpeaker',
                      http_method='GET', name='getFeaturedSpeaker')
    def getFeaturedSpeaker(self, request):
        """Gets the featured speaker for the desired conference."""

        if ndb.Key(urlsafe=request.conferenceKey).kind() != "Conference":
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.conferenceKey)

        cachedSpeaker = memcache.get(
            'featuredSpeaker-' +
            request.conferenceKey)
        if not cachedSpeaker:
            q = ConferenceSession.query(
                ancestor=ndb.Key(
                    urlsafe=request.conferenceKey))
            q = q.order(ConferenceSession.createdTime)

            featuredSpeaker = None
            speakers = {}
            for cs in q:
                if cs.speaker in speakers:
                    featuredSpeaker = cs.speaker
                    speakers[cs.speaker].append(cs.name)
                else:
                    speakers[cs.speaker] = [cs.name]

            if featuredSpeaker:
                cachedSpeaker = (featuredSpeaker, speakers[featuredSpeaker])
                memcache.set(
                    'featuredSpeaker-' +
                    request.conferenceKey,
                    cachedSpeaker)

        response = GetFeaturedSpeakerResponse()
        if cachedSpeaker:
            response.speaker = contents[0]
            response.sessionNames = contents[1]

        return response

# - - - Registration - - - - - - - - - - - - - - - - - - - -

    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser()  # get user Profile

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
        prof = self._getProfileFromUser()  # get user Profile
        conf_keys = [ndb.Key(urlsafe=wsck)
                     for wsck in prof.conferenceKeysToAttend]
        conferences = ndb.get_multi(conf_keys)

        # get organizers
        organisers = [ndb.Key(Profile, conf.organizerUserId)
                      for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[
                self._copyConferenceToForm(
                    conf,
                    names[
                        conf.organizerUserId]) for conf in conferences])

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
        q = q.filter(Conference.city == "London")
        q = q.filter(Conference.topics == "Medical Innovations")
        q = q.filter(Conference.month == 6)

        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") for conf in q]
        )

# - - - Conference Sessions - - - - - - - - - - - - - - - - - - - -

    @endpoints.method(CREATE_CSESSION_REQ, ConferenceSessionForm,
                      path='createSession',
                      http_method='POST', name='createSession')
    def createSession(self, request):
        """Create new conference session."""
        return self._createConferenceSessionObject(request)

    def _createConferenceSessionObject(self, request):
        """Create ConferenceSession object."""

        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # extract session form
        data = {
            field.name: getattr(
                request,
                field.name) for field in request.all_fields()}
        del data['conferenceKey']

        # check that conference exists
        confKey = ndb.Key(urlsafe=request.conferenceKey)
        if confKey.kind() != "Conference":
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.conferenceKey)

        conf = confKey.get()

        # check that user is owner
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the owner can add sessions to a conference.')

        # convert dates from strings to Date objects; set month based on
        # start_date
        data['date'] = datetime.strptime(data['date'][:10], "%Y-%m-%d").date()
        data['typeOfSession'] = str(data['typeOfSession'])

        cs_id = ConferenceSession.allocate_ids(size=1, parent=confKey)[0]
        cs_key = ndb.Key(ConferenceSession, cs_id, parent=confKey)
        data['key'] = cs_key
        data['createdTime'] = int(calendar.timegm(time.gmtime()))

        cs = ConferenceSession(**data).put()

        # add a task to update the featured speaker
        taskqueue.add(
            params={
                'speaker': request.speaker,
                'conferenceKey': request.conferenceKey},
            url='/tasks/update_featured_speaker')

        return self._copyConferenceSessionToForm(cs.get())

    @endpoints.method(GET_CSESSION_BY_SPEAKER_REQ, ConferenceSessionForms,
                      path='getSessionsBySpeaker',
                      http_method='GET',
                      name='getSessionsBySpeaker')
    def getSessionsBySpeaker(self, request):
        """Retrieves sessions matching the request query"""
        q = ConferenceSession.query(
            ConferenceSession.speaker == request.speaker)

        return ConferenceSessionForms(
            items=[self._copyConferenceSessionToForm(cs) for cs in q]
        )

    @endpoints.method(GET_CSESSION_BY_TYPE_REQ, ConferenceSessionForms,
                      path='getConferenceSessionsByType',
                      http_method='GET',
                      name='getConferenceSessionsByType')
    def getConferenceSessionsByType(self, request):
        """Retrieves sessions matching the request query"""
        q = ConferenceSession.query(ancestor=ndb.Key(
            urlsafe=request.conferenceKey))

        q = q.filter(
            ConferenceSession.typeOfSession == str(
                request.typeOfSession))

        return ConferenceSessionForms(
            items=[self._copyConferenceSessionToForm(cs) for cs in q]
        )

    @endpoints.method(GET_CSESSION_BY_CID_REQ, ConferenceSessionForms,
                      path='getConferenceSessions',
                      http_method='GET',
                      name='getConferenceSessions')
    def getConferenceSessionsByConfId(self, request):
        """Retrieves sessions matching the request query"""

        q = ConferenceSession.query(ancestor=ndb.Key(
            urlsafe=request.conferenceKey))

        return ConferenceSessionForms(
            items=[self._copyConferenceSessionToForm(cs) for cs in q]
        )

    def _copyConferenceSessionToForm(self, confSession):
        """
        Copy relevant fields from ConferenceSession to ConferenceSessionForm.
        """

        csf = ConferenceSessionForm()
        for field in csf.all_fields():
            if hasattr(confSession, field.name):
                if field.name == "date":
                    setattr(
                        csf, field.name, str(
                            getattr(
                                confSession, field.name)))
                elif field.name == "typeOfSession":
                    setattr(
                        csf,
                        field.name,
                        getattr(
                            ConferenceSessionType,
                            getattr(
                                confSession,
                                field.name)))
                else:
                    setattr(csf, field.name, getattr(confSession, field.name))
            elif field.name == "sessionKey":
                setattr(csf, field.name, confSession.key.urlsafe())

        csf.check_initialized()
        return csf

# - - - Sessions Wishlists - - - - - - - - - - - - - - - - - - - -

    @endpoints.method(CREATE_WISHLIST_ITEM_REQ, BooleanMessage,
                      path='addSessionToWishlist',
                      http_method='POST', name='addSessionToWishlist')
    def addSessionToWishlist(self, request):
        """Create new conference session."""
        return self._createSessionWishlistObject(request)

    def _createSessionWishlistObject(self, request):
        """Wishlists a session for the current user."""

        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # check that session exists
        sessionKey = ndb.Key(urlsafe=request.sessionKey)
        if sessionKey.kind() != "ConferenceSession":
            raise endpoints.NotFoundException(
                'No session found with key: %s' % request.sessionKey)

        csession = sessionKey.get()

        q = SessionWishlistItem.query()
        q = q.filter(SessionWishlistItem.userId == user_id)
        q = q.filter(SessionWishlistItem.sessionKey == request.sessionKey)

        # if session has already been wishlisted by this user, simply return
        # success
        if q.count() > 0:
            return BooleanMessage(data=True)

        wlItem = SessionWishlistItem()
        wlItem.userId = user_id
        wlItem.sessionKey = request.sessionKey
        wlItem.put()

        return BooleanMessage(data=True)

    @endpoints.method(message_types.VoidMessage, ConferenceSessionForms,
                      path='getSessionsInWishlist',
                      http_method='GET',
                      name='getSessionsInWishlist')
    def getSessionsInWishlist(self, request):
        """Gets all sessions the current user has wishlisted"""

        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        session_keys = [
            wl.sessionKey for wl in SessionWishlistItem.query(
                SessionWishlistItem.userId == user_id)]
        wl_sessions = [ndb.Key(urlsafe=k).get() for k in session_keys]

        return ConferenceSessionForms(
            items=[self._copyConferenceSessionToForm(cs) for cs in wl_sessions]
        )

api = endpoints.api_server([ConferenceApi])  # register API