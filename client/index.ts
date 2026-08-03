import {startApp} from 'superdesk-core/scripts/index';

setTimeout(() => {
    startApp(
        [
            // Always-on / manually-managed extensions. Entries with custom load
            // logic (.then, setCustomizations) live here so scripts/dev/extension.sh
            // can manipulate the standard-template region below without worrying
            // about them.
            {
                id: 'planning-extension',
                load: () => import('superdesk-planning/client/planning-extension'),
            },
            // NOTE: the 'broadcasting' extension (develop default) is intentionally
            // NOT enabled. It evaluates rundown field definitions at import time via
            // getVocabulary(RUNDOWN_ITEM_TYPES_VOCABULARY_ID).display_name; PesaCheck's
            // vocabularies.json has no rundown vocabularies, so that read throws during
            // extension load, the extensionsHaveLoaded event never fires, and the app is
            // pinned on a blank loading screen. PesaCheck is a fact-checking newsroom and
            // does not use broadcasting/rundowns. Re-enable only after seeding the
            // rundown_item_types / rundown_subitem_types / cameras vocabularies.

            // extensions:start (managed by scripts/dev/extension.sh — do not edit by hand)
            {
                id: 'annotationsLibrary',
                load: () => import('superdesk-core/scripts/extensions/annotationsLibrary'),
            },
            {
                id: 'markForUser',
                load: () => import('superdesk-core/scripts/extensions/markForUser'),
            },
            {
                id: 'datetimeField',
                load: () => import('superdesk-core/scripts/extensions/datetimeField'),
            },
            // 'availability-manager' (develop default) removed alongside 'broadcasting':
            // it manages staff availability for broadcast shows/rundowns, which PesaCheck
            // does not use. Re-enable together with broadcasting if rundowns are adopted.
            // extensions:end
        ],
        {},
    );
});

export default angular.module('main.superdesk', []);
