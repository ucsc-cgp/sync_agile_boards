import datetime
from more_itertools import first
import re
import requests

from settings import transitions
from src.access import get_access_params
from src.issue import Repo, Issue
from src.utilities import CrypticNames, get_zenhub_pipeline


class JiraRepo(Repo):

    def __init__(self, repo_name, jira_org, issues: list = None):
        """Create a Project storing all issues belonging to the provided project key
        :param repo_name: Required. The repo to work with e.g. TEST
        :param jira_org: Required. The organization the repo belongs to, e.g. ucsc-cgl
        :param issues: Optional. If not specified, all issues in the repo will be retrieved. If specified, only will
        retrieve and update the listed issues.
        """

        super().__init__()
        self.url = get_access_params('jira')['options']['server'] % jira_org
        self.headers = {'Authorization': 'Basic ' + get_access_params('jira')['api_token']}

        self.name = repo_name
        self.org = jira_org
        self.issues = dict()

        if issues:
            for issue in issues:
                self.issues[issue] = JiraIssue(key=issue, repo=self)

        else:  # By default, get all issues
            self.api_call()  # Get information for all issues in the project

        # self.github_org, self.github_repo = board_map[jira_org][repo]

    def api_call(self, start=0, updated_since: datetime = None):
        """
        Make API calls until all results have been retrieved. Jira API responses can be paginated, defaulting to 50
        results per page, so a new call has to be made until the total is reached.

        :param start: The index in the results to start at. Always call this function with start=0
        :param updated_since: If specified, get just the issues that have been updated since the time given in this
        datetime object. Otherwise, get all issues in the repo.
        """

        if updated_since:  # format the timestamp to use in a Jira query
            timestamp_filter = f" AND updated>='{updated_since.strftime('%Y-%m-%d %H:%M')}'"
        else:
            timestamp_filter = ''  # otherwise do not filter by timestamp

        response = requests.get(f'{self.url}search?jql=project={self.name}{timestamp_filter}&startAt={str(start)}',
                                headers=self.headers).json()

        for issue in response['issues']:
            self.issues[issue['key']] = JiraIssue(content=issue, repo=self)

        if response['total'] >= start + response['maxResults']:  # There could be another page of results
            self.api_call(start=start + response['maxResults'], updated_since=updated_since)


class JiraIssue(Issue):

    # TODO break up this huge method
    def __init__(self, repo: 'JiraRepo', key: str = None, content: dict = None):
        """
        Create an Issue object from an issue key or from a portion of an API response

        :param repo: The JiraRepo object representing the repo this issue belongs to
        :param key: If specified, make an API call searching by this issue key
        :param content: If specified, don't make a new API call but use this response from an earlier one
        """

        super().__init__()

        self.repo = repo

        if key:
            response = requests.get(f'{self.repo.url}search?jql=id={key}', headers=self.repo.headers)

            if response.status_code == 200:
                json = response.json()
            else:
                raise ValueError(f'{response.status_code} Error: {response.text}')

            if 'issues' in json.keys():  # If the key doesn't match any issues, this will be an empty list
                content = json['issues'][0]  # Get the one and only issue in the response
            else:
                raise ValueError('No issue matching this id was found')

        if content['fields']['assignee']:
            self.assignees = [content['fields']['assignee']['name']]
        self.description = content['fields']['description']
        self.issue_type = content['fields']['issuetype']['name']
        self.jira_key = content['key']
        self.status = content['fields']['status']['name']

        self.summary = content['fields']['summary']

        # Convert the timestamps into datetime objects and localize them to PST time
        self.updated = datetime.datetime.strptime(content['fields']['updated'].split('.')[0],
                                                  '%Y-%m-%dT%H:%M:%S').replace(
            tzinfo=JiraIssue.get_utc_offset(content['fields']['updated']))

        # Not all issue descriptions have the corresponding github issue listed in them
        self.github_repo, self.github_key = self.get_github_equivalent() or (None, None)

        if CrypticNames.story_points in content['fields'].keys():
            self.story_points = content['fields'][CrypticNames.story_points]

        if CrypticNames.sprint in content['fields']:  # This custom field holds sprint information
            if content['fields'][CrypticNames.sprint]:
                # This field is a list containing a dictionary that's been put in string format.
                # Sprints can have duplicate names. id is the unique identifier used by the API.

                match_obj = re.search(r'id=(\w*),', content['fields']['customfield_10010'][0])
                if match_obj:
                    self.jira_sprint = int(match_obj.group(1))
                else:
                    print('No sprint name was found in the sprint field')

        self.pipeline = get_zenhub_pipeline(self)  # This must be done after sprint status is set

    @staticmethod
    def get_utc_offset(timestamp):
        """Return a timezone object representing the UTC offset found in the timestamp"""
        offset_direction = timestamp[-5]  # A plus or minus sign
        offset_hours = int(timestamp[-4:-2])
        offset_minutes = int(timestamp[-2:])
        offset_seconds = offset_hours * 3600 + offset_minutes * 60
        return datetime.timezone(datetime.timedelta(seconds=int(offset_direction + str(offset_seconds))))

    def get_github_equivalent(self):
        """Find the equivalent Github issue key and repo name if listed in the issue text. Issues synced by unito-bot
        will have this information."""

        if self.description:
            match_obj = re.search(r'Repository Name: ([\w_-]*)[\s\S]*Issue Number: ([\w-]*)', self.description)
            if match_obj:
                return match_obj.group(1), match_obj.group(2)
            print(self.jira_key, 'No match was found in the description.')

    def update_remote(self):
        """Update the remote issue. The issue must already exist in Jira."""

        transition = {'transition': {'id': transitions[self.status]}}

        # Issue status has to be updated as a transition
        r = requests.post(f'{self.repo.url}issue/{self.jira_key}/transitions', headers=self.repo.headers, json=transition)

        if r.status_code != 204:  # HTTP 204 No Content on success
            print(f'{r.status_code} Error transitioning')

        # Story points field can be updated from a dictionary
        if self.story_points:  # Do not try to update this if there is no value for story points
            r = requests.put(f'{self.repo.url}issue/{self.jira_key}', headers=self.repo.headers,
                             json={'fields': {CrypticNames.story_points: self.story_points}})

            if r.status_code != 204:  # HTTP 204 No Content on success
                print(f'{r.status_code} Error updating Jira: {r.text}')

    def post_new_issue(self):
        """Post this issue to Jira for the first time. The issue must not already exist."""

        r = requests.post(f'{self.repo.url}issue/', headers=self.repo.headers, json=self.dict_format())

        if r.status_code != 201:  # HTTP 201 means created
            print(f'{r.status_code} Error posting to Jira: {r.text}')

        self.jira_key = r.json()['key']  # keep the key that Jira assigned to this issue when creating it

    def change_epic_membership(self, add: str = None, remove: str = None):
        """Add or remove given issue from this epic (self). Specify one issue to add or remove as a kwarg"""

        if add and not remove:
            epic_name = self.jira_key
        elif remove and not add:
            epic_name = 'none'
        else:
            raise RuntimeError('change_epic_membership must be called with exactly one argument')

        issues = {'issues': [add or remove]}
        old_api_url = first(self.repo.url.split('api'))  # remove 'api/latest' from the url
        r = requests.post(f'{old_api_url}agile/1.0/epic/{epic_name}/issue', json=issues, headers=self.repo.headers)

        if r.status_code != 204:  # HTTP 204 on success
            print(f'{r.status_code} Error changing Jira epic membership: {r.text}')

    def get_epic_children(self):
        """If this issue is an epic, get all its children"""
        r = requests.get(f"{self.repo.url}search?jql=cf[10008]='{self.jira_key}'", headers=self.repo.headers)

        if r.status_code == 200:  # HTTP 200 OK
            children = [i['key'] for i in r.json()['issues']]
            return children
        else:
            print(f'{r.status_code} Error getting Jira epic children: {r.text}')

if __name__ == '__main__':
    j = JiraRepo(repo_name='TEST', jira_org='ucsc-cgl', issues=['TEST-3'])
