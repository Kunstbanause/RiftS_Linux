// Headless OpenXR client: plays a vibration script with exact timing.
// Script lines: <t_ms> <L|R> <amplitude 0..1> <duration_ms>   (amplitude 0 = stop)
// Usage: hapticsplay <script> [repeat]   (needs a focused session: no game running)
// Build: gcc -O1 -Wall hapticsplay.c -o hapticsplay -lopenxr_loader
#include <time.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <openxr/openxr.h>

#define CHK(x) do { XrResult r_ = (x); if (XR_FAILED(r_)) { fprintf(stderr, "FAIL %s = %d\n", #x, r_); exit(1); } } while (0)

struct event { double t_ms; int hand; float amplitude; double duration_ms; };

static XrInstance inst;
static XrSession sess;
static XrActionSet set;
static XrAction buzz;
static XrPath hands[2];
static int state = XR_SESSION_STATE_UNKNOWN;

static double now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1e3 + ts.tv_nsec / 1e6;
}

static void sleep_until_ms(double t)
{
	struct timespec ts = {(time_t)(t / 1e3), (long)((t - (time_t)(t / 1e3) * 1e3) * 1e6)};
	clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL);
}

static void pump(void)
{
	XrEventDataBuffer ev = {XR_TYPE_EVENT_DATA_BUFFER};
	while (xrPollEvent(inst, &ev) == XR_SUCCESS) {
		if (ev.type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED) {
			state = ((XrEventDataSessionStateChanged *)&ev)->state;
			if (state == XR_SESSION_STATE_READY) {
				XrSessionBeginInfo bi = {XR_TYPE_SESSION_BEGIN_INFO};
				CHK(xrBeginSession(sess, &bi));
			}
		}
		ev = (XrEventDataBuffer){XR_TYPE_EVENT_DATA_BUFFER};
	}
	XrActiveActionSet aas = {set, XR_NULL_PATH};
	XrActionsSyncInfo si = {XR_TYPE_ACTIONS_SYNC_INFO, NULL, 1, &aas};
	xrSyncActions(sess, &si);
}

static void play(const struct event *e)
{
	XrHapticActionInfo hi = {XR_TYPE_HAPTIC_ACTION_INFO, NULL, buzz, hands[e->hand]};
	if (e->amplitude <= 0.0f) {
		CHK(xrStopHapticFeedback(sess, &hi));
		return;
	}
	XrHapticVibration v = {XR_TYPE_HAPTIC_VIBRATION};
	v.amplitude = e->amplitude;
	v.duration = (XrDuration)(e->duration_ms * 1e6);
	v.frequency = XR_FREQUENCY_UNSPECIFIED;
	CHK(xrApplyHapticFeedback(sess, &hi, (XrHapticBaseHeader *)&v));
}

int main(int argc, char **argv)
{
	if (argc < 2) {
		fprintf(stderr, "usage: hapticsplay <script> [repeat]\n");
		return 2;
	}
	int repeat = argc > 2 ? atoi(argv[2]) : 1;
	FILE *f = fopen(argv[1], "r");
	if (f == NULL) {
		perror(argv[1]);
		return 2;
	}
	int n = 0, cap = 1024;
	struct event *ev = malloc(cap * sizeof(*ev));
	char side;
	while (fscanf(f, "%lf %c %f %lf", &ev[n].t_ms, &side, &ev[n].amplitude, &ev[n].duration_ms) == 4) {
		ev[n].hand = side == 'R';
		if (++n == cap)
			ev = realloc(ev, (cap *= 2) * sizeof(*ev));
	}
	fclose(f);
	if (n == 0) {
		fprintf(stderr, "empty script\n");
		return 2;
	}

	const char *exts[] = {"XR_MND_headless"};
	XrInstanceCreateInfo ici = {XR_TYPE_INSTANCE_CREATE_INFO};
	strcpy(ici.applicationInfo.applicationName, "hapticsplay");
	ici.applicationInfo.apiVersion = XR_MAKE_VERSION(1, 0, 0);
	ici.enabledExtensionCount = 1;
	ici.enabledExtensionNames = exts;
	CHK(xrCreateInstance(&ici, &inst));
	XrSystemGetInfo sgi = {XR_TYPE_SYSTEM_GET_INFO, NULL, XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY};
	XrSystemId sys;
	CHK(xrGetSystem(inst, &sgi, &sys));
	XrSessionCreateInfo sci = {XR_TYPE_SESSION_CREATE_INFO, NULL, 0, sys};
	CHK(xrCreateSession(inst, &sci, &sess));

	XrActionSetCreateInfo asci = {XR_TYPE_ACTION_SET_CREATE_INFO};
	strcpy(asci.actionSetName, "hapticsplay");
	strcpy(asci.localizedActionSetName, "hapticsplay");
	CHK(xrCreateActionSet(inst, &asci, &set));
	xrStringToPath(inst, "/user/hand/left", &hands[0]);
	xrStringToPath(inst, "/user/hand/right", &hands[1]);
	XrActionCreateInfo aci = {XR_TYPE_ACTION_CREATE_INFO};
	strcpy(aci.actionName, "buzz");
	strcpy(aci.localizedActionName, "buzz");
	aci.actionType = XR_ACTION_TYPE_VIBRATION_OUTPUT;
	aci.countSubactionPaths = 2;
	aci.subactionPaths = hands;
	CHK(xrCreateAction(set, &aci, &buzz));
	XrPath prof, lp, rp;
	xrStringToPath(inst, "/interaction_profiles/oculus/touch_controller", &prof);
	xrStringToPath(inst, "/user/hand/left/output/haptic", &lp);
	xrStringToPath(inst, "/user/hand/right/output/haptic", &rp);
	XrActionSuggestedBinding b[2] = {{buzz, lp}, {buzz, rp}};
	XrInteractionProfileSuggestedBinding sb = {XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING, NULL, prof, 2, b};
	CHK(xrSuggestInteractionProfileBindings(inst, &sb));
	XrSessionActionSetsAttachInfo att = {XR_TYPE_SESSION_ACTION_SETS_ATTACH_INFO, NULL, 1, &set};
	CHK(xrAttachSessionActionSets(sess, &att));

	for (double t = now_ms() + 3000; state != XR_SESSION_STATE_FOCUSED && now_ms() < t;) {
		pump();
		sleep_until_ms(now_ms() + 12);
	}
	if (state != XR_SESSION_STATE_FOCUSED) {
		fprintf(stderr, "session never got focus (state %d): close the game / other OpenXR apps\n", state);
		return 1;
	}

	for (int r = 0; r < repeat; r++) {
		printf("playing %d events, %.1f s (%d/%d)\n", n, ev[n - 1].t_ms / 1e3, r + 1, repeat);
		fflush(stdout);
		double start = now_ms() + 500;
		for (int i = 0; i < n; i++) {
			// Keep syncing (focus, Monado's own pulse stops) while waiting for the next event
			double due = start + ev[i].t_ms;
			while (now_ms() < due - 4) {
				pump();
				sleep_until_ms(now_ms() + 4 < due ? now_ms() + 4 : due);
			}
			sleep_until_ms(due);
			play(&ev[i]);
		}
		for (int h = 0; h < 2; h++) {
			struct event stop = {0, h, 0, 0};
			play(&stop);
		}
		double end = now_ms() + 1500;
		while (now_ms() < end) {
			pump();
			sleep_until_ms(now_ms() + 12);
		}
	}

	xrDestroyInstance(inst);
	return 0;
}
