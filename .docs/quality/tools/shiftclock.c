/* Сдвиг системных часов для одного процесса: перехват clock_gettime и time.
   Нужен для разового замера — какие тесты держатся на календаре машины. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdlib.h>
#include <time.h>

static long shift_seconds(void) {
    static long cached = -1;
    if (cached == -1) {
        const char *days = getenv("CLOCK_SHIFT_DAYS");
        cached = days ? atol(days) * 86400L : 0L;
    }
    return cached;
}

int clock_gettime(clockid_t id, struct timespec *ts) {
    static int (*real)(clockid_t, struct timespec *) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "clock_gettime");
    int rc = real(id, ts);
    if (rc == 0 && (id == CLOCK_REALTIME || id == CLOCK_REALTIME_COARSE))
        ts->tv_sec += shift_seconds();
    return rc;
}

time_t time(time_t *t) {
    static time_t (*real)(time_t *) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "time");
    time_t value = real(NULL) + shift_seconds();
    if (t) *t = value;
    return value;
}

int gettimeofday(struct timeval *tv, void *tz) {
    static int (*real)(struct timeval *, void *) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "gettimeofday");
    int rc = real(tv, tz);
    if (rc == 0 && tv) tv->tv_sec += shift_seconds();
    return rc;
}
