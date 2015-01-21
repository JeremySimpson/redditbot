import re
import time
import logging
import random

import snudown
from bs4 import BeautifulSoup

import redditbot.base.utils as utils
from redditbot.base.handlers import MailTriggeredBot, UserCommentsVoteTriggeredBot, SubredditCommentTriggeredBot, \
    SubredditSubmissionTriggeredBot

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

FULL_EMOTE_REGEX = re.compile(
    """\[[^\]]*\]\s*\(\s*/(?P<name>[^\s/]+?)(?P<modifier>-\S+)?\s*(?P<message>["'].*?["'])?\s*\)""")
PONY_SUBS = ["mylittlepony", "mlplounge", "ploungeafterdark", "mylittlefriends", "mylittleandysonic1"]
PONY_SECRETS = [u'[](/adorkable "%s")']

REDDIT_PM_IGNORE = "http://reddit.com/message/compose/?to=xkcd_transcriber&subject=ignore%20me&message=ignore%20me"
REDDIT_PM_DELETE = "http://reddit.com/message/compose/?to=xkcd_transcriber&subject=delete&message=delete%20{thing_id}"
NO_BREAK_SPACE = u'\u00A0'
MAX_MESSAGE_LENGTH = 10000

XKCD_SIG_LINKS = [
    u'[xkcd.com](http://www.xkcd.com)',
    u'[xkcd%ssub](http://www.reddit.com/r/xkcd/)' % NO_BREAK_SPACE,
    u'[Problems/Bugs?](http://www.reddit.com/r/xkcd_transcriber/)',
    u'[Statistics](http://xkcdref.info/statistics/)',
    u'[Stop%sReplying](%s)' % (NO_BREAK_SPACE, REDDIT_PM_IGNORE),
    u'[Delete](%s)' % REDDIT_PM_DELETE
]


class MailXkcdBot(MailTriggeredBot):
    def __init__(self, *args, **kwargs):
        self.datastore = kwargs.pop('datastore')
        self.xkcd_fetcher = kwargs.pop('xkcd_fetcher')
        super(MailXkcdBot, self).__init__(*args, **kwargs)

    def _check(self, mail):
        if utils.has_replied(mail, self.auth['username']):
            return False
        if utils.is_comment_owner(mail, self.auth['username']):
            return False
        return True

    def _do(self, mail):
        body_lower = mail.body.lower()
        subject_lower = mail.subject.lower()
        result = True

        if self.is_private_message(mail):
            if body_lower.find('ignore me') != -1 or subject_lower.find('ignore me') != -1:
                result = self.process_ignore(mail)
            elif body_lower.startswith('delete') or subject_lower.startswith('delete'):
                result = self.process_delete(mail)
        elif self.is_comment_reply(mail):
            result = self.process_comment_reply(mail)

        if result:
            mail.mark_as_read()
        return result

    def process_ignore(self, mail):
        # Add to ignore list
        self.datastore.add_ignore(mail.author.name.lower())

        # Reply to the user
        reply_msg = "You have been added to the ignore list. If this bot continues to respond, PM /u/LunarMist2."
        if utils.send_reply(mail, reply_msg):
            return True
        return False

    def process_delete(self, mail):
        # Ensure the mail author is the same as the original referencer
        parts = mail.body.split(' ')
        if len(parts) == 2:
            thing_id = parts[1]
            obj = self.r.get_info(thing_id=thing_id)
            if obj:
                parent = self.r.get_info(thing_id=obj.parent_id)
                if parent and parent.author and parent.author.name == mail.author.name:
                    obj.delete()
                    logger.info(' => Comment Deleted!')

        return True

    def process_comment_reply(self, mail):
        body_lower = mail.body.lower()

        # Check for joke replies
        if body_lower.find('thank you') != -1 or body_lower.find('thanks') != -1:
            reply_msg = "[](/sbstalkthread)My pleasure"
        elif body_lower.find('i love you') != -1:
            reply_msg = "[](/sbstalkthread)Love ya too~"
        elif body_lower == 'k':
            reply_msg = "[](/o_o)K"
        elif body_lower == ")":
            reply_msg = "("
        else:
            return True

        # Do not reply if the user is ignored
        if mail.author and mail.author.name.lower() in self.datastore.get_ignores():
            logger.info('Skipping mail {id}. Reason: Author on ignore list.'.format(id=mail.id))
            return True

        # Reply to the user
        if utils.send_reply(mail, reply_msg):
            return True
        return False


class VoteXkcdBot(UserCommentsVoteTriggeredBot):
    def _do(self, comment):
        logger.info('Comment {id} below score threshold: {score}. Removing'.format(id=comment.id, score=comment.score))
        comment.delete()
        return True


class CommentXkcdBot(SubredditCommentTriggeredBot):
    def __init__(self, *args, **kwargs):
        self.datastore = kwargs.pop('datastore')
        self.xkcd_fetcher = kwargs.pop('xkcd_fetcher')
        super(CommentXkcdBot, self).__init__(*args, **kwargs)

    def _check(self, comment):
        if comment.body.lower().find('xkcd.com') == -1:
            return False
        if comment.subreddit.display_name.lower().find('xkcd') != -1:
            return False
        if comment.subreddit.display_name.lower() == 'jerktalkdiamond':
            return False
        if utils.is_comment_owner(comment, self.auth['username']):
            return False
        if utils.has_replied(comment, self.auth['username']):
            return False
        return not utils.has_chain(self.r, comment, self.auth['username'])

    def _do(self, comment):
        html = snudown.markdown(comment.body.encode('UTF-8'))
        soup = BeautifulSoup(html)
        refs = {}

        # Iterate through all links, get xkcd json
        for link in soup.find_all('a'):
            href = link.get('href')
            if not href:
                continue
            j = self.xkcd_fetcher.get_json(href)
            if not j:
                logger.warn('Data could not be fetched for {url}'.format(url=href))
                continue
            refs[int(j.get('num', -1))] = {
                'data': j,
                'href': href
            }

        return self.process_references(comment, refs)

    def process_references(self, comment, refs):
        if not refs:
            return True

        # Record in db the references
        for comic_id, ref in refs.iteritems():
            if comic_id > 0:
                timestamp = int(time.time())
                author = comment.author.name if comment.author else '[deleted]'
                sub = comment.subreddit.display_name
                link = comment.permalink
                self.datastore.insert_xkcd_event(comic_id, timestamp, sub, author, link,
                                                 ref['data'].get('from_external', False))

        # Do not reply if the user is ignored
        if comment.author and comment.author.name.lower() in self.datastore.get_ignores():
            logger.info('Skipping comment {id}. Reason: Author on ignore list.'.format(id=comment.id))
            return True

        return self.send_reply(comment, refs)

    def send_reply(self, comment, refs):
        # Check for secret message
        secret_message = ''
        matches = re.findall(FULL_EMOTE_REGEX, comment.body)
        if matches:
            for match in matches:
                d = match.groupdict()
                if d['message'] and d['message'].find('xkcd_transcriber') != -1:
                    secret_message = "Hello, " + comment.author.name if comment.author else "[deleted]"
                    break

        # Secret emote
        secret_emote = ''
        if comment.subreddit.display_name.lower() in PONY_SUBS or secret_message:
            secret_emote = random.choice(PONY_SECRETS) % secret_message + ' '

        # Start building the message
        reply_msg_head = secret_emote
        reply_msg_sig = '---\n' + ' ^| '.join(['^' + a for a in XKCD_SIG_LINKS])
        reply_msg_body = ''

        # Build body text
        for comic_id, ref in refs.iteritems():
            data = ref['data']
            if reply_msg_body != '':
                reply_msg_body += u'----\n'

            if ref['href'].find('imgs.xkcd.com') != -1 or data.get('from_external') is True:
                reply_msg_body += u'[Original Source](http://xkcd.com/{num}/)\n\n'.format(num=comic_id)
            elif data.get('img'):
                reply_msg_body += u'[Image]({image})\n\n'.format(
                    image=data.get('img').replace('(', '\\(').replace(')', '\\)'))
            if data.get('link'):
                reply_msg_body += u'[Link]({link})\n\n'.format(
                    link=data.get('link').replace('(', '\\(').replace(')', '\\)'))
            if data.get('title'):
                reply_msg_body += u'**Title:** {title}\n\n'.format(title=data.get('title', '').replace('\n', '\n\n'))
            if data.get('alt'):
                reply_msg_body += u'**Title-text:** {alt}\n\n'.format(alt=data.get('alt', '').replace('\n', '\n\n'))
            if comic_id > 0:
                explained = self.xkcd_fetcher.get_explained_link(comic_id)
                reply_msg_body += u'[Comic Explanation]({link})\n\n'.format(link=explained)

            stats = self.datastore.get_stats(comic_id)
            if stats:
                plural = 's' if stats['count'] != 1 else ''
                reply_msg_body += u'**Stats:** This comic has been referenced {0} time{1}, representing {2:.4f}% of referenced xkcds.\n\n'.format(
                    stats['count'], plural, stats['percentage'])

        # Do not send if there's no body
        if len(reply_msg_body.strip()) == 0:
            return True

        # Reply to the user
        reply_msg = reply_msg_head + reply_msg_body + reply_msg_sig
        reply_obj = utils.send_reply(comment, reply_msg)
        if reply_obj is None:
            return False

        # Edit and fix [delete] signature link
        reply_msg_sig = '---\n' + ' ^| '.join(['^' + a for a in XKCD_SIG_LINKS]).format(thing_id=reply_obj.name)
        reply_msg = reply_msg_head + reply_msg_body + reply_msg_sig
        if not utils.edit_reply(reply_obj, reply_msg):
            return False

        return True


class SubmissionXkcdBot(SubredditSubmissionTriggeredBot):
    def __init__(self, *args, **kwargs):
        self.datastore = kwargs.pop('datastore')
        self.xkcd_fetcher = kwargs.pop('xkcd_fetcher')
        super(SubmissionXkcdBot, self).__init__(*args, **kwargs)

    def _check(self, submission):
        if submission.is_self:
            if submission.selftext.lower().find('xkcd.com') == -1:
                return False
        else:
            if submission.url.lower().find('xkcd.com') == -1:
                return False
        if submission.subreddit.display_name.lower().find('xkcd') != -1:
            return False
        if submission.subreddit.display_name.lower() == 'jerktalkdiamond':
            return False
        if utils.is_comment_owner(submission, self.auth['username']):
            return False
        if utils.has_replied(submission, self.auth['username']):
            return False
        return not utils.has_chain(self.r, submission, self.auth['username'])

    def _do(self, submission):
        if submission.is_self:
            return self.process_self(submission)
        else:
            return self.process_link(submission)

    def process_self(self, submission):
        html = snudown.markdown(submission.selftext.encode('UTF-8'))
        soup = BeautifulSoup(html)
        refs = {}

        # Iterate through all links, get xkcd json
        for link in soup.find_all('a'):
            href = link.get('href')
            if not href:
                continue
            j = self.xkcd_fetcher.get_json(href)
            if not j:
                logger.warn('Data could not be fetched for {url}'.format(url=href))
                continue
            refs[int(j.get('num', -1))] = {
                'data': j,
                'href': href
            }

        return self.process_references(submission, refs)

    def process_link(self, submission):
        # Only need to process a single url
        j = self.xkcd_fetcher.get_json(submission.url)
        if not j:
            logger.warn('Data could not be fetched for {url}'.format(url=submission.url))
            return True
        refs = {
            int(j.get('num', -1)): {
                'data': j,
                'href': submission.url
            }
        }

        return self.process_references(submission, refs)

    def process_references(self, submission, refs):
        if not refs:
            return True

        # Record in db the references
        for comic_id, ref in refs.iteritems():
            if comic_id > 0:
                timestamp = int(time.time())
                author = submission.author.name if submission.author else '[deleted]'
                sub = submission.subreddit.display_name
                link = submission.permalink
                self.datastore.insert_xkcd_event(comic_id, timestamp, sub, author, link,
                                                 ref['data'].get('from_external', False))

        # Do not reply if the user is ignored
        if submission.author and submission.author.name.lower() in self.datastore.get_ignores():
            logger.info('Skipping submission {id}. Reason: Author on ignore list.'.format(id=submission.id))
            return True

        return self.send_reply(submission, refs)

    def send_reply(self, submission, refs):
        # Check for secret message
        secret_message = ''
        matches = re.findall(FULL_EMOTE_REGEX, getattr(submission, 'selftext', ''))
        if matches:
            for match in matches:
                d = match.groupdict()
                if d['message'] and d['message'].find('xkcd_transcriber') != -1:
                    secret_message = "Hello, " + submission.author.name if submission.author else "[deleted]"
                    break

        # Secret emote
        secret_emote = ''
        if submission.subreddit.display_name.lower() in PONY_SUBS or secret_message:
            secret_emote = random.choice(PONY_SECRETS) % secret_message + ' '

        # Start building the message
        reply_msg_head = secret_emote
        reply_msg_sig = '---\n' + ' ^| '.join(['^' + a for a in XKCD_SIG_LINKS])
        reply_msg_body = ''

        # Build body text
        for comic_id, ref in refs.iteritems():
            data = ref['data']
            if reply_msg_body != '':
                reply_msg_body += u'----\n'

            if ref['href'].find('imgs.xkcd.com') != -1 or data.get('from_external') is True:
                reply_msg_body += u'[Original Source](http://xkcd.com/{num}/)\n\n'.format(num=comic_id)
            elif data.get('img'):
                reply_msg_body += u'[Image]({image})\n\n'.format(
                    image=data.get('img').replace('(', '\\(').replace(')', '\\)'))
            if data.get('link'):
                reply_msg_body += u'[Link]({link})\n\n'.format(
                    link=data.get('link').replace('(', '\\(').replace(')', '\\)'))
            if data.get('title'):
                reply_msg_body += u'**Title:** {title}\n\n'.format(title=data.get('title', '').replace('\n', '\n\n'))
            if data.get('transcript'):
                reply_msg_body += u'**Transcript:** {transcript}\n\n'.format(
                    transcript=re.sub('\n{{.+}}', '', data.get('transcript', '')).replace('\n', '\n\n'))
            if data.get('alt'):
                reply_msg_body += u'**Title-text:** {alt}\n\n'.format(alt=data.get('alt', '').replace('\n', '\n\n'))
            if comic_id > 0:
                explained = self.xkcd_fetcher.get_explained_link(comic_id)
                reply_msg_body += u'[Comic Explanation]({link})\n\n'.format(link=explained)

            stats = self.datastore.get_stats(comic_id)
            if stats:
                plural = 's' if stats['count'] != 1 else ''
                reply_msg_body += u'**Stats:** This comic has been referenced {0} time{1}, representing {2:.4f}% of referenced xkcds.\n\n'.format(
                    stats['count'], plural, stats['percentage'])

        # Do not send if there's no body
        if len(reply_msg_body.strip()) == 0:
            return True

        # Reply to the user
        reply_msg = reply_msg_head + reply_msg_body + reply_msg_sig
        reply_obj = utils.send_reply(submission, reply_msg)
        if reply_obj is None:
            return False

        # Edit and fix [delete] signature link
        reply_msg_sig = '---\n' + ' ^| '.join(['^' + a for a in XKCD_SIG_LINKS]).format(thing_id=reply_obj.name)
        reply_msg = reply_msg_head + reply_msg_body + reply_msg_sig
        if not utils.edit_reply(reply_obj, reply_msg):
            return False

        return True