Udacity Full-Stack Web Developer Nanodegree Project 4: Conference Application
=============================================================================

This project is based on the conference application starter code provided by Udacity.
Please see README_Udacity.MD to view original README file. The only requirement
necessary to run tee deployed version of this application is access to Google's 
appspot servers and a Google+ login for functionality resulting in data updates 
(creating conferences, etc).

AppSpot project id: esoteric-grove-95615
Base application: http://esoteric-grove-95615.appspot.com/
Application API: http://esoteric-grove-95615.appspot.com/_ah/api/explorer

Required project enhancements are outlined in the following document:
* https://docs.google.com/a/knowlabs.com/document/d/1H9anIDV4QCPttiQEwpGe6MnMBx92XCOlz0B4ciD7lOs/pub

Tasks requiring additional information are outlined below.

Conference Sessions are created as a child of Conferences. In addition to listed
requisite fields, a createdTime field was added which stores the number of ticks
since the epoch. This is useful in some queries, such as the getFeaturedSpeaker() 
endpoint.

Wishlists are implemented as userId / sessionKey pairs, analogous to a junction table
in traditional relational database design, and stored as SessionWishlistItem
entities in the DataStore. Wishlisting a session does not require the user be signed up 
for the conferenec in which that session exists. This is done so that a user can use 
wishlisted sessions as a decision point in whether to attend a conference. This decision
also drives one of the queries created as part of Task 3, explained later.

Speakers are represented as strings. This can lead to the possibility of ambiguity
if more than one speaker with the same name is scheduled to speak at a conference,
but the organizer can limit its impact by including additional identifying information 
in the speaker field, e.g., John Smith (Chair of Mathematics, Udacity).

The two additional queries implemented to fulfill Task 3 are descrbied below:

1. getConferencesWithWishlistedSessions() - retrieves conferences having sessions 
wishlisted by the current user, ordered descending by number of wishlisted sessions.

2. getConferenceSpeakers(conferenceKey) - retrieves all speakers at a given conference.

Also in Task 3, a question was posed to identify why a query for non-workshop sessions
before 7pm would be problematic using Google DataStore. This is an issue because only one
inequality filter can be applied to a query. However, this is easily solved by retrieving
results of the first inequality (e.g., ConferenceSession.typeOfSession != "WORKSHOP") and
then looping through them, applying additional inequality filters directly in Python.

For Task 4, a conference can have only one featured speaker at a time and the featured 
speaker is updated any time a session is added. This means featured speakers can change
and at any given time will be the speaker of the most-recently added session who also
has at least 2 sessions.

A few potential future enhancements for this application:
* Session timing validation. For example, scheduling a speaker in two concurrent sessions
would probably be undesired, depending on session complexity (multiple speakers, etc). Or a
session can be booked that falls outside the bounds of the conference.
* Queries could ignore past conferences / sessions.
* More robust speaker representation, eliminating problems from duplicate names.
* Better featured speaker rules.