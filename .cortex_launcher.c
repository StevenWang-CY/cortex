/* Minimal launcher for the Cortex daemon.
   Compiled into CortexDaemon.app so macOS gives it its own TCC identity
   (com.cortex.daemon) for camera access.

   The project root is derived from this binary's location:
     CortexDaemon.app/Contents/MacOS/CortexDaemon → project root is 4 levels up.

   Retries chdir on failure because macOS may need to show a Desktop folder
   access dialog the first time this app runs.

   Build:
     cc -o CortexDaemon.app/Contents/MacOS/CortexDaemon .cortex_launcher.c
     codesign --force --deep --sign - CortexDaemon.app
*/
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <libgen.h>
#include <mach-o/dyld.h>

int main(void) {
    /* Resolve project root from executable path:
       .../Ralph/CortexDaemon.app/Contents/MacOS/CortexDaemon
       → dirname 4x → .../Ralph */
    char exe_path[4096];
    uint32_t size = sizeof(exe_path);
    if (_NSGetExecutablePath(exe_path, &size) != 0) {
        fprintf(stderr, "ERROR: Could not get executable path\n");
        return 1;
    }

    /* Resolve symlinks */
    char *real = realpath(exe_path, NULL);
    if (!real) {
        perror("realpath");
        return 1;
    }

    /* Walk up 4 directory levels: MacOS → Contents → CortexDaemon.app → project */
    char *dir = real;
    for (int i = 0; i < 4; i++) {
        dir = dirname(dir);
    }

    char project[4096];
    strncpy(project, dir, sizeof(project) - 1);
    project[sizeof(project) - 1] = '\0';
    free(real);

    /* Build paths relative to project root */
    char python[4096], logfile[4096];
    snprintf(python,  sizeof(python),  "%s/.venv/bin/python", project);
    snprintf(logfile, sizeof(logfile), "%s/cortex_daemon.log", project);

    /* Retry chdir — macOS TCC may prompt for Desktop folder access.
       Give the user up to 15 seconds to click Allow. */
    int cd_ok = 0;
    for (int i = 0; i < 15; i++) {
        if (chdir(project) == 0) {
            cd_ok = 1;
            break;
        }
        sleep(1);
    }

    /* Redirect stdout/stderr to log file (use /tmp if project dir inaccessible) */
    const char *actual_log = cd_ok ? logfile : "/tmp/cortex_daemon.log";
    FILE *log = fopen(actual_log, "a");
    if (log) {
        dup2(fileno(log), STDOUT_FILENO);
        dup2(fileno(log), STDERR_FILENO);
        fclose(log);
    }

    if (!cd_ok) {
        fprintf(stderr, "ERROR: Could not chdir to %s after 15s\n", project);
        return 1;
    }

    char *argv[] = {python, "-m", "cortex.scripts.run_dev", NULL};
    execv(python, argv);

    /* execv only returns on error */
    perror("execv");
    return 1;
}
