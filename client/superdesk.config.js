/**
 * This is the default configuration file for the Superdesk application. By default,
 * the app will use the file with the name "superdesk.config.js" found in the current
 * working directory, but other files may also be specified using relative paths with
 * the SUPERDESK_CONFIG environment variable or the grunt --config flag.
 */
module.exports = function() {
    return {
        apps: [
            'superdesk-publisher',
            'superdesk-planning',
            'superdesk.analytics',
        ],
        importApps: [
            '../index',
            'superdesk-publisher',
            'superdesk-planning',
            'superdesk-analytics',
        ],

        defaultRoute: '/workspace/monitoring',

        publisher: {
            protocol: 'https',                /* http or https */
            tenant: '',              /* tenant - semantically subdomain, '' is allowed */
            domain: '',           /* domain name for the publisher */
            base: 'api/v2',                  /* api base path */

            wsProtocol: 'wss',                /* ws or wss (websocket); if unspecified or '' defaults to 'wss' */
            wsDomain: '',  /* domain name (usually domain as above) */
                                            /* e.g.: example.com, abc.example.com */
                                            /* tenant, as above, is NOT used for websocket */
            wsPath: '/ws',                    /* path to websocket root dir */
            wsPort: '80',                   /* if not specified: defaults to 443 for wss, 80 for ws */
            hideContentRoutesInPublishPane: false, /* hides routes of type "content" from select box in publish panes in monitoring view as well as in output control. If not specified: defaults to false */
            hideCustomRoutesInPublishPane: false   /* hides routes of type "custom" from select box in publish panes in monitoring view as well as in output control. If not specified: defaults to false */
        },

        langOverride: {
            en: {
                'ANPA Category': 'Category',
                'ANPA CATEGORY': 'CATEGORY',
            }
        },

        view: {
            timeformat: 'HH:mm',
            dateformat: 'YYYY-MM-DD',
        },

        shortTimeFormat: 'HH:mm, YYYY-MM-DD',
        shortDateFormat: 'HH:mm, YYYY-MM-DD',
        shortWeekFormat: 'HH:mm, YYYY-MM-DD',
        startingDay: '1',
        defaultTimezone: 'Europe/Prague',

        editor3: { browserSpellCheck: true, },

        search_cvs: [
            {id: 'topics', name:'Topics', field: 'subject', list: 'topics'},
            {id: 'language', name:'Language', field: 'language', list: 'languages'},
            {id: 'project', name:'Project', field: 'subject', list: 'priority'},
            {id: 'claimformat', name:'Claim format', field: 'subject', list: 'claimformat'},
            {id: 'claimtopic', name:'Claim topic', field: 'subject', list: 'Harm_type'},
            {id: 'claimtype', name:'Claim type', field: 'subject', list: 'claimtype'},
            {id: 'countrymention1', name:'Primary country', field: 'subject', list: 'countrymention1'},
            {id: 'countrymention2nd', name:'2nd country mention', field: 'subject', list: 'countrymention2'},
            {id: 'countrymention3rd', name:'3rd country mention', field: 'subject', list: 'countrymention3'},
            {id: 'countrymention4th', name:'4th country mention', field: 'subject', list: 'countrymention4'},
            {id: 'countrymention5th', name:'5th country mention', field: 'subject', list: 'countrymention5'},
            {id: 'countriesmention', name:'Countries mentioned', field: 'subject', list: 'countries'},
            {id: 'debunklanguage', name:'Debunk language', field: 'subject', list: 'Debunklang'},
            {id: 'debunkrating', name:'Debunk rating', field: 'subject', list: 'Debunk'},
            {id: 'geccategory', name:'GEC category', field: 'subject', list: 'GEC'},
            {id: 'harmtype', name:'Harm type', field: 'subject', list: '01harm'},
            {id: 'primaryplatform', name:'Primary platform', field: 'subject', list: 'platform'},
        ],

        features: {
            preview: 1,
            swimlane: {columnsLimit: 99},
            swimlane: {defaultNumberOfColumns: 4},
            editor3: true,
            editorHighlights: true,
            noPublishOnAuthoringDesk: true,
            sendToPersonal: true,
            customAuthoringTopbar: {
                toDesk: true,
                publish: true,

                sendAndDuplicate: {
                    deskName: 'Test',
                    stageName: 'Working',
                },
            },
            validatePointOfInterestForImages: true,
            editorHighlights: true,
            editFeaturedImage: true,
            searchShortcut: true,
            elasticHighlight: true,
            planning: true,
            autorefreshContent: true,
            nestedItemsInOutputStage: false,
            planning: true,
        },

        item_profile: { change_profile: 1 },

        workspace: {
            planning: true,
            assignments: true,
            analytics: true,
        },

        ui: {
            italicAbstract: false,
            },

        list: {
            priority: [
                'urgency'
            ],
            firstLine: [
                'headline',
                'highlights',
                'markedDesks',
                'associatedItems',
                'versioncreated'
            ],
            secondLine: [
                'state',
                'update',
                'scheduledDateTime',
                'embargo',
                'flags',
                'updated',
                'provider',
                'desk',
                'fetchedDesk',
                'used',
                'nestedlink',
                'translations'
            ],
            compactView: {
                firstLine: [
                    'headline',
                    'slugline',
                ],
                secondLine: [],
            },
        },

        monitoring: {
            scheduled: {
                sort: {
                    default: { field: 'publish_schedule', order: 'asc' },
                    allowed_fields_to_sort: [ 'publish_schedule' ]
                }
            },
        },
    };
};
