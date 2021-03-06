#!/usr/bin/python

import atexit
import os
import json
import urllib
import sys
import collections
import dateutil.parser
import requests
import subprocess
import httplib
import sqlite3
import datetime
import scraperwiki
import random
import codecs
import fcntl

from secrets import *

# Horrendous hack to work around some Twitter / Python incompatibility
# http://bobrochel.blogspot.co.nz/2010/11/bad-servers-chunked-encoding-and.html


def patch_http_response_read(func):
    def inner(*args):
        try:
            return func(*args)
        except httplib.IncompleteRead, e:
            return e.partial
    return inner
httplib.HTTPResponse.read = patch_http_response_read(httplib.HTTPResponse.read)

# Make sure you install this version of "twitter":
# http://pypi.python.org/pypi/twitter
# http://mike.verdone.ca/twitter/
# https://github.com/sixohsix/twitter
import twitter

#########################################################################
# Logging

logf = open(os.path.expanduser("~/all.log"), 'a', buffering=1)


def log(message):
    """
    Write message to the log file with a timestamp and a pid and
    a final newline.
    """

    timestamp = datetime.datetime.now().isoformat()
    pid = os.getpid()

    logf.write("{} {} {}\n".format(timestamp, pid, message))


def on_exit():
    log("exiting (via atexit)")

atexit.register(on_exit)

log("started with arguments: {!r}".format(sys.argv))
log("started with environ: ONETIME={!r}, MODE={!r}".format(
    os.environ.get("ONETIME"),
    os.environ.get("MODE")))

#########################################################################
# Authentication to Twitter

# This is designed to, when good, be submitted as a patch to add to
# twitter.oauth_dance (which currently only has a function for PIN
# authentication, not redirect)
from twitter.api import Twitter
from twitter.oauth import OAuth, write_token_file, read_token_file
from twitter.oauth_dance import parse_oauth_tokens


def oauth_url_dance(consumer_key, consumer_secret, callback_url,
                    oauth_verifier, pre_verify_token_filename,
                    verified_token_filename):
    # Verification happens in two stages...

    # 1) If we haven't done a pre-verification yet... Then we get credentials
    # from Twitter that will be used to sign our redirect to them, find the
    # redirect, and instruct the Javascript that called us to do the redirect.
    if not os.path.exists(CREDS_PRE_VERIFIY):
        twitter = Twitter(auth=OAuth('', '', consumer_key, consumer_secret),
                          format='', api_version=None)
        oauth_token, oauth_token_secret = parse_oauth_tokens(
            twitter.oauth.request_token(oauth_callback=callback_url))
        write_token_file(pre_verify_token_filename, oauth_token,
                         oauth_token_secret)

        oauth_url = 'https://api.twitter.com/oauth/authorize?' + \
            urllib.urlencode({'oauth_token': oauth_token})
        return oauth_url

    # 2) We've done pre-verification, hopefully the user has authed us in
    # Twitter and we've been redirected to. Check we are and ask for the
    # permanent tokens.
    oauth_token, oauth_token_secret = read_token_file(CREDS_PRE_VERIFIY)
    twitter = Twitter(auth=OAuth(oauth_token, oauth_token_secret,
                                 consumer_key, consumer_secret),
                      format='', api_version=None)
    oauth_token, oauth_token_secret = parse_oauth_tokens(
        twitter.oauth.access_token(oauth_verifier=oauth_verifier))
    write_token_file(verified_token_filename, oauth_token, oauth_token_secret)
    return oauth_token, oauth_token_secret


def do_tool_oauth():
    if not os.path.exists(CREDS_VERIFIED):
        if len(sys.argv) < 3:
            result = "need-oauth"
        else:
            (callback_url, oauth_verifier) = (sys.argv[1], sys.argv[2])
            result = oauth_url_dance(CONSUMER_KEY, CONSUMER_SECRET,
                                     callback_url, oauth_verifier,
                                     CREDS_PRE_VERIFIY, CREDS_VERIFIED)
        # a string means a URL for a redirect
        # (otherwise we get a tuple back with auth tokens in)
        if type(result) == str:
            set_status_and_exit('auth-redirect', 'error',
                                'Permission needed from Twitter',
                                {'url': result})

    oauth_token, oauth_token_secret = read_token_file(CREDS_VERIFIED)
    tw = twitter.Twitter(auth=twitter.OAuth(oauth_token, oauth_token_secret,
                                            CONSUMER_KEY, CONSUMER_SECRET))
    return tw


# Afer detecting an auth failed error mid work, call this
def clear_auth_and_restart():
    # remove auth files and respawn
    try:
        os.remove(CREDS_PRE_VERIFIY)
        os.remove(CREDS_VERIFIED)
    except OSError:
        # don't worry if the files aren't there
        pass
    subprocess.call(sys.argv)
    sys.exit()

#########################################################################
# Helper functions

COLUMNS = collections.OrderedDict([["id_str", unicode()],
                                   ["tweet_url", unicode()],
                                   ["created_at", datetime.datetime.now()],
                                   ["text", unicode()],
                                   ["lang", unicode()],
                                   ["retweet_count", int(0)],
                                   ["favorite_count", int(0)],
                                   ["screen_name", unicode()],
                                   ["in_reply_to_screen_name", unicode()],
                                   ["in_reply_to_status_id",  unicode()],
                                   ["lat", float(0)],
                                   ["lng", float(0)],
                                   ["user_location", unicode()],
                                   ["user_time_zone", unicode()],
                                   ["urls", unicode()],
                                   ["media", unicode()],
                                   ["user_mentions", unicode()],
                                   ["hashtags", unicode()],
                                   ["query", unicode()]])


# Converts a list of strings into a space separated string
def make_space_separated_or_none(l, field, prefix=""):
    if not l:
        return None

    plucked = [prefix + i.get(field, '') for i in l]
    return " ".join(plucked)


# Signal back to the calling Javascript, to the database,
# and custard's status API, our status
def set_status_and_exit(status, typ, message, extra={}):
    requests.post("https://scraperwiki.com/api/status",
                  data={'type': typ, 'message': message})

    data = {'id': 'tweets', 'current_status': status}
    scraperwiki.sql.save(['id'], data, table_name='__status')

    log("set_status_and_exit status={!r}, type={!r}, message={!r}".format(
        status, typ, message))

    extra['status'] = status
    print json.dumps(extra)
    sys.exit()


# Either we or the user is changing the mode explicitly
def change_mode(new_mode):
    log("change_mode new_mode={!r}".format(new_mode))
    scraperwiki.sql.save(['id'],
                         {'id': 'tweets', 'mode': new_mode},
                         table_name='__mode')
    # mode changed, so do stuff again
    crontab_install()


# Read mode from database
def get_mode():
    try:
        mode = scraperwiki.sql.select('mode from __mode')[0]['mode']
    except sqlite3.OperationalError:
        # legacy place mode is stored
        try:
            mode = scraperwiki.sql.select('mode from __status')[0]['mode']
            # save in new place
            change_mode(mode)
        except sqlite3.OperationalError:
            # happens when '__mode' table doesn't exist, so make it
            mode = 'clearing-backlog'
            change_mode(mode)
    # convert legacy mode
    if mode == 'backlog-cleared':
        mode = 'clearing-backlog'
    log("initial mode = {!r}".format(mode))
    assert mode in ['clearing-backlog', 'monitoring']  # should never happen
    return mode


# The range of Tweets we're currently fetching, from end backwards
def change_window(start, end):
    scraperwiki.sql.save(['id'],
                         {'id': 'tweets', 'window_start': start,
                          'window_end': end},
                         table_name='__window')
    log("new window! window = {!r} - {!r}".format(start, end))


def get_max_id_ever_seen_expensive():
    """
    This query is expensive because sqlite can't have indices on
    computed fields. We have to have the field in string form for javascript.
    It's here as a legacy, we should only need to compute it once ever for
    a dataset and then never again.
    """
    log("get_max_id_ever_seen_expensive called!")
    try:
        return scraperwiki.sql.select(
            "max(cast(id_str as integer)) as max_id from tweets")[0]["max_id"]
    except sqlite3.OperationalError:
        return None


def get_max_id_ever_seen():
    """
    Return maximum id ever seen as int, or None if we've never seen records.
    """
    try:
        return scraperwiki.sql.select(
            'max_id_seen from __max_id')[0]['max_id_seen']
    except sqlite3.OperationalError:
        return get_max_id_ever_seen_expensive()


def set_max_id_ever_seen(max_id_ever_seen):
    data = {'id': 'max_id_seen', 'max_id_seen': max_id_ever_seen}
    scraperwiki.sql.save(['id'], data, table_name='__max_id')


def process_results(results, query_terms):
    datas = []
    for tweet in results['statuses']:
        data = collections.OrderedDict()
        # XXX any new entries here should be reflected in COLUMNS XXX #
        data['id_str'] = str(tweet['id_str'])
        data['tweet_url'] = "https://twitter.com/" + \
            tweet['user']['screen_name'] + "/status/" + str(tweet['id_str'])
        data['created_at'] = dateutil.parser.parse(tweet['created_at'])

        data['text'] = tweet['text']
        data['lang'] = tweet['lang']

        data['retweet_count'] = tweet['retweet_count']
        data['favorite_count'] = tweet['favorite_count']
        # conversation thread length?

        data['screen_name'] = tweet['user']['screen_name']
        data['in_reply_to_screen_name'] = tweet['in_reply_to_screen_name']
        data['in_reply_to_status_id'] = tweet['in_reply_to_status_id_str']

        if 'geo' in tweet and tweet['geo'] is not None \
                and 'coordinates' in tweet['geo']:
            data['lat'] = tweet['geo']['coordinates'][0]
            data['lng'] = tweet['geo']['coordinates'][1]

        data['user_location'] = tweet['user']['location']
        data['user_time_zone'] = tweet['user']['time_zone']

        entities = tweet.get('entities', {})

        urls = entities.get('urls')
        data['urls'] = make_space_separated_or_none(urls, u'expanded_url')
        media = entities.get('media')
        data['media'] = make_space_separated_or_none(media, u'media_url_https')
        users = entities.get('user_mentions')
        data['user_mentions'] = make_space_separated_or_none(
            users, u'screen_name', u'@')
        hashtags = entities.get('hashtags')
        data['hashtags'] = make_space_separated_or_none(
            hashtags, u'text', u'#')

        data['query'] = query_terms

        datas.append(data)

    scraperwiki.sql.save(['id_str'], datas, table_name="tweets")

    if datas:
        min_id = min(int(x['id_str']) for x in datas)
        max_id = max(int(x['id_str']) for x in datas)
        log("about to save; min_id {}, max_id {}".format(min_id, max_id))
        prev_max = get_max_id_ever_seen()
        new_max = max(prev_max, max_id)
        set_max_id_ever_seen(new_max)
    else:
        log("no datas")

    return len(results['statuses'])


# make a new crontab file, with random minute to distribute load for platform
def crontab_install():
    if not os.path.isfile("crontab"):
        crontab = open("tool/crontab.template").read()
        crontab = crontab.replace("RANDOM", str(random.randint(0, 59)))
        open("crontab", "w").write(crontab)
    # implement whatever crontab has been written to the crontab text file
    # (this may or may not be different to the existing crontab)
    os.system("crontab crontab")


#########################################################################
# Commands
def command_change_mode():
    # Just change the mode, then stop
    assert 'MODE' in os.environ
    mode = os.environ['MODE']
    change_mode(mode)
    print json.dumps({'mode-changed': 'ok'})
    sys.exit()


def command_clean_state():
    # Clean everything, as if the tool was new
    scraperwiki.sql.execute("drop table if exists tweets")
    scraperwiki.sql.execute("drop table if exists __status")
    scraperwiki.sql.execute("drop table if exists __max_id")
    scraperwiki.sql.dt.create_table(COLUMNS, 'tweets')
    change_mode('clearing-backlog')
    os.system("crontab -r >/dev/null 2>&1")
    change_window(None, None)
    set_status_and_exit('clean-slate', 'error', 'No query set')
    sys.exit()


def command_diagnostics():
    # Diagnostic information only, e.g. rate limiting status

    # connect to Twitter - TODO, send something appropriate back if this fails
    tw = do_tool_oauth()

    diagnostics = {}
    diagnostics['_rate_limit_status'] = tw.application.rate_limit_status()
    diagnostics['limit'] = diagnostics['_rate_limit_status']['resources']['search']['/search/tweets']['limit']
    diagnostics['remaining'] = diagnostics['_rate_limit_status']['resources']['search']['/search/tweets']['remaining']
    diagnostics['reset'] = diagnostics['_rate_limit_status']['resources']['search']['/search/tweets']['reset']
    diagnostics['_account_settings'] = tw.account.settings()
    diagnostics['user'] = diagnostics['_account_settings']['screen_name']

    statuses = scraperwiki.sql.select('* from __status')[0]
    diagnostics['status'] = statuses['current_status']

    modes = scraperwiki.sql.select('* from __mode')[0]
    diagnostics['mode'] = modes['mode']

    windows = scraperwiki.sql.select('* from __window')[0]
    diagnostics['window_start'] = windows.get('window_start', None)
    diagnostics['window_end'] = windows.get('window_end', None)

    crontab = subprocess.check_output("crontab -l | grep twsearch.py; true",
                                      stderr=subprocess.STDOUT, shell=True)
    diagnostics['crontab'] = crontab

    print json.dumps(diagnostics)
    sys.exit()


def make_sure_string(var):
    # Make something a string, without casting None to string
    if var is None:
        return var
    return str(var)


def command_scrape(mode):
    # Make sure this scrape mode only runs once at once
    f = open("query.txt")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        log("already running scrape according to flock, exiting")
        sys.exit()

    # Get query we're working on from file we store it in
    query_terms = codecs.open("query.txt", "r", "utf-8").read().strip()

    # Read window from database
    try:
        window_start = make_sure_string(
            scraperwiki.sql.select(
                'window_start from __window')[0]['window_start'])
    except sqlite3.OperationalError:
        try:
            window_start = make_sure_string(
                scraperwiki.sql.select(
                    'window_start from __status')[0]['window_start'])
        except sqlite3.OperationalError:
            window_start = None
    try:
        window_end = make_sure_string(
            scraperwiki.sql.select(
                'window_end from __window')[0]['window_end'])
    except sqlite3.OperationalError:
        try:
            window_end = make_sure_string(
                scraperwiki.sql.select(
                    'window_end from __status')[0]['window_end'])
        except sqlite3.OperationalError:
            window_end = None
    log("initial window = {!r} - {!r}".format(window_start, window_end))

    onetime = 'ONETIME' in os.environ

    pages_got = 0
    try:
        # Make the tweets table *first* with dumb data, calling DumpTruck
        # directly, so it appears before the status one in the list
        scraperwiki.sql.dt.create_table(COLUMNS, 'tweets')

        # Connect to Twitter
        tw = do_tool_oauth()

        # crontab to schedule for next time
        crontab_install()

        # Loop termination: Note that we search with max_id set to the id of
        # some tweet that we have already saved, which means we'll get that
        # tweet in our results, which means that we only have _new_ tweets if
        # the number that we got is bigger than 1.
        got = 2
        while got > 1:
            log("q = {!r} window = {!r} - {!r}".format(
                query_terms, window_start, window_end))
            if window_end is None:
                log("    jumping forwards")
                # for some reason can't just pass max_id in as None
                results = tw.search.tweets(q=query_terms, result_type='recent', count=100,
                                           since_id=window_start)
            else:
                log("    filling in backwards")
                results = tw.search.tweets(q=query_terms, result_type='recent', count=100,
                                           max_id=window_end,
                                           since_id=window_start)

            got = process_results(results, query_terms)
            log("    got {}".format(got))

            if got > 0:
                window_end = make_sure_string(
                    min(x['id'] for x in results['statuses']))
                change_window(window_start, window_end)

            pages_got += 1
            if onetime:
                break

        # Update the window, it now starts from most recent place forward
        # (i.e. window_end is None)
        if not onetime:
            # The double cast here is so SQLite correctly sorts id_str as
            # if it were an integer not a string, yet we still return a string
            window_start = make_sure_string(get_max_id_ever_seen())
            change_window(window_start, None)

        # Get the mode again, in case the user has meanwhile
        # changed it by clicking "Monitor future tweets"
        mode = get_mode()
        if not onetime and mode == 'clearing-backlog':
            # We've reached as far back as we'll ever get,
            # so we're done forever stop the crontab
            os.system("crontab -r >/dev/null 2>&1")
            set_status_and_exit("ok-updating", 'ok', '')

        # In monitoring mode, the next run we'll jump forward again
        # as window_end is now None

    except twitter.api.TwitterHTTPError, e:
        if "Twitter sent status 401 for URL" in str(e):
            clear_auth_and_restart()

        # https://dev.twitter.com/docs/error-codes-responses
        obj = json.loads(e.response_data)
        code = obj['errors'][0]['code']
        # authentication failure
        if (code in [32, 89]):
            clear_auth_and_restart()
        # rate limit exceeded
        if code == 34:
            set_status_and_exit('not-there', 'error', 'User not on Twitter')
        if code == 195:
            set_status_and_exit('invalid-query', 'error',
                                "That isn't a valid Twitter search")
        if code == 44:
            set_status_and_exit('near-not-supported', 'error',
                                "Twitter's API doesn't support NEAR")
        if code == 88:
            # provided we got at least one page, rate limit isn't an error
            # but expected
            if pages_got == 0:
                set_status_and_exit('rate-limit', 'error',
                                    'Twitter has asked us to slow down')
        else:
            # anything else is an unexpected error
            # if ones occur a lot, add the above instead
            raise
    except httplib.IncompleteRead, e:
        # I think this is effectively a rate limit error
        # so only count if it was first error
        if pages_got == 0:
            set_status_and_exit('rate-limit', 'error',
                                'Twitter broke the connection')

    # Save progress message
    set_status_and_exit("ok-updating", 'ok', '')


#########################################################################
# Main code

# Parameters to this command vary:
#   a. None: try and scrape Twitter followers
#   b. callback_url oauth_verifier:
#      have just come back from Twitter with these oauth tokens
#   c. "clean-slate": wipe database and start again

command = 'scrape'
if len(sys.argv) > 1:
    if sys.argv[1] in ('change-mode', 'clean-slate', 'diagnostics'):
        command = sys.argv[1]

mode = get_mode()

if command == 'change-mode':
    command_change_mode()
elif command == 'clean-slate':
    command_clean_state()
elif command == 'diagnostics':
    command_diagnostics()
elif command == 'scrape':
    command_scrape(mode)
else:
    assert False
