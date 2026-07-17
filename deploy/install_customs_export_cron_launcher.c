#define _GNU_SOURCE

/*
 * Required bootstrap contract:
 *   cc -std=c11 -O2 -Wall -Wextra -Werror -static -Wl,--build-id=none
 * Build outside the mutable service-owned repository and pin the reviewed
 * launcher binary SHA-256 out of band. The trusted root operator must copy the
 * prebuilt launcher and installer into root-owned temporary files, verify both
 * approved digests after the copy, fsync each file and STATE_DIR, and only then
 * atomically rename them into place as root:root 0700. Never compile or execute
 * a service-owned repository path as root. The launcher additionally pins the
 * exact installer digest on every invocation. Before acquiring the install
 * lock, direct invocations re-exec themselves through a unique transient
 * systemd scope with Delegate=yes; raw cgroup creation in an undelegated unit
 * or session is unsupported and rejected.
 */

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <linux/magic.h>
#include <sys/file.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/vfs.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define STATE_DIR "/var/lib/sanjuk-stock-simulator"
#define LAUNCHER_PATH STATE_DIR "/install-customs-export-cron-launcher"
#define INSTALLER_PATH STATE_DIR "/install-customs-export-cron.sh"
#define LOCK_PATH STATE_DIR "/install.lock"
#define SHA256SUM_PATH "/usr/bin/sha256sum"
#define SYSTEMD_RUN_PATH "/usr/bin/systemd-run"
#define CGROUP_ROOT "/sys/fs/cgroup"
#define DELEGATED_SCOPE_PREFIX "sanjuk-customs-export-installer-"
#define CGROUP_PAYLOAD_NAME "_payload"
#define DELEGATION_NONCE_ENV "SANJUK_CUSTOMS_DELEGATION_NONCE"
#define SELF_CGROUP_PATH "/proc/self/cgroup"
#define INSTALLER_SHA256 \
    "532f325ef697703f204ed0ab064aa3b4413467be82d06ab5dcbd76326a644191"
#define INSTALLER_SIZE ((off_t)20397)
#define REQUIRED_UID ((uid_t)0)
#define REQUIRED_GID ((gid_t)0)
#define INSTALL_TIMEOUT_SECONDS 60U
#define KILL_GRACE_SECONDS 5U

static volatile sig_atomic_t child_process_group = -1;
static volatile sig_atomic_t kill_pending = 0;
static volatile sig_atomic_t timed_out = 0;
static volatile sig_atomic_t forwarded_signal = 0;
static char delegated_cgroup_relative[PATH_MAX + 1];

/*
 * Keep the trusted, digest-pinned installer tree in a private cgroup. The
 * installer must not migrate itself or descendants out of that cgroup; this is
 * descendant containment, not a sandbox against hostile root code. The cgroup
 * directory FD is the stable identity, avoiding numeric PGID reuse after an
 * abrupt launcher death.
 */
struct installer_cgroup {
    int directory_fd;
    char path[PATH_MAX + 1];
};

static int emit_launcher_event(const char *status, int ok, int exit_code) {
    printf("{\"installer_status\":\"%s\",\"ok\":%s}\n", status,
           ok ? "true" : "false");
    return exit_code;
}

static int fail_preflight(void) {
    return emit_launcher_event("launcher_preflight_failed", 0, 1);
}

static int secure_path(const char *path, mode_t mode, int directory) {
    struct stat st;

    if (lstat(path, &st) != 0) {
        return 0;
    }
    if (directory ? !S_ISDIR(st.st_mode) : !S_ISREG(st.st_mode)) {
        return 0;
    }
    if (!directory && st.st_nlink != 1) {
        return 0;
    }
    return st.st_uid == REQUIRED_UID && st.st_gid == REQUIRED_GID &&
           (st.st_mode & 07777) == mode;
}

static int verify_installer_digest(void) {
    struct stat installer;
    int output_pipe[2];
    int status = 0;
    pid_t child;
    char output[128];
    size_t used = 0;
    int read_ok = 1;

    if (lstat(INSTALLER_PATH, &installer) != 0 ||
        installer.st_size != INSTALLER_SIZE ||
        !secure_path(SHA256SUM_PATH, 0755, 0) ||
        pipe2(output_pipe, O_CLOEXEC) != 0) {
        return 0;
    }
    child = fork();
    if (child < 0) {
        close(output_pipe[0]);
        close(output_pipe[1]);
        return 0;
    }
    if (child == 0) {
        char *const argv[] = {(char *)SHA256SUM_PATH, (char *)"--",
                              (char *)INSTALLER_PATH, NULL};
        close(output_pipe[0]);
        if (dup2(output_pipe[1], STDOUT_FILENO) < 0) {
            _exit(127);
        }
        close(output_pipe[1]);
        execv(SHA256SUM_PATH, argv);
        _exit(127);
    }

    close(output_pipe[1]);
    for (;;) {
        char buffer[128];
        ssize_t length = read(output_pipe[0], buffer, sizeof(buffer));
        if (length == 0) {
            break;
        }
        if (length < 0) {
            if (errno == EINTR) {
                continue;
            }
            read_ok = 0;
            break;
        }
        size_t available = sizeof(output) - 1U - used;
        size_t copy_length =
            (size_t)length < available ? (size_t)length : available;
        if (copy_length > 0) {
            memcpy(output + used, buffer, copy_length);
            used += copy_length;
        }
    }
    close(output_pipe[0]);
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) {
            return 0;
        }
    }
    output[used] = '\0';
    return read_ok && WIFEXITED(status) && WEXITSTATUS(status) == 0 &&
           used >= 65U && memcmp(output, INSTALLER_SHA256, 64U) == 0 &&
           (output[64] == ' ' || output[64] == '\t');
}

static int running_from_approved_inode(void) {
    char path[PATH_MAX + 1];
    struct stat executable;
    struct stat approved;
    ssize_t length = readlink("/proc/self/exe", path, PATH_MAX);

    if (length < 0 || length > PATH_MAX) {
        return 0;
    }
    path[length] = '\0';
    if (strcmp(path, LAUNCHER_PATH) != 0 ||
        stat("/proc/self/exe", &executable) != 0 ||
        lstat(LAUNCHER_PATH, &approved) != 0) {
        return 0;
    }
    return executable.st_dev == approved.st_dev &&
           executable.st_ino == approved.st_ino;
}

static void close_extra_fds(void) {
#ifdef SYS_close_range
    if (syscall(SYS_close_range, 3U, ~0U, 0U) == 0) {
        return;
    }
#endif
    DIR *directory = opendir("/proc/self/fd");
    if (directory != NULL) {
        int directory_fd = dirfd(directory);
        struct dirent *entry;
        while ((entry = readdir(directory)) != NULL) {
            char *end = NULL;
            long fd = strtol(entry->d_name, &end, 10);
            if (end != entry->d_name && *end == '\0' && fd > 2 &&
                fd != directory_fd && fd <= INT_MAX) {
                close((int)fd);
            }
        }
        closedir(directory);
        return;
    }

    long maximum = sysconf(_SC_OPEN_MAX);
    if (maximum < 0) {
        maximum = 1024;
    }
    for (long fd = 3; fd < maximum; ++fd) {
        close((int)fd);
    }
}

static int acquire_install_lock(void) {
    int lock_fd = open(LOCK_PATH,
                       O_RDWR | O_CREAT | O_NOFOLLOW | O_NONBLOCK |
                           O_CLOEXEC,
                       0600);
    struct stat metadata;

    if (lock_fd < 0 || fstat(lock_fd, &metadata) != 0 ||
        !S_ISREG(metadata.st_mode) || metadata.st_uid != REQUIRED_UID ||
        metadata.st_gid != REQUIRED_GID ||
        (metadata.st_mode & 07777) != 0600 || metadata.st_nlink != 1) {
        if (lock_fd >= 0) {
            close(lock_fd);
        }
        return -1;
    }
    if (flock(lock_fd, LOCK_EX | LOCK_NB) != 0) {
        int saved_errno = errno;
        close(lock_fd);
        if (saved_errno == EWOULDBLOCK || saved_errno == EAGAIN) {
            return -2;
        }
        return -1;
    }
    return lock_fd;
}

static void signal_child_group(int signal_number) {
    if (child_process_group > 0) {
        kill(-child_process_group, signal_number);
    }
}

static int read_bounded_fd(int descriptor, char *buffer, size_t capacity) {
    size_t used = 0;

    while (used + 1U < capacity) {
        ssize_t length = read(descriptor, buffer + used, capacity - 1U - used);
        if (length == 0) {
            buffer[used] = '\0';
            return 1;
        }
        if (length < 0) {
            if (errno == EINTR) {
                continue;
            }
            return 0;
        }
        used += (size_t)length;
    }
    char extra;
    ssize_t length;
    do {
        length = read(descriptor, &extra, 1);
    } while (length < 0 && errno == EINTR);
    if (length != 0) {
        return 0;
    }
    buffer[used] = '\0';
    return 1;
}

static int read_cgroup_file_at(const struct installer_cgroup *scope,
                               const char *name, char *buffer,
                               size_t capacity) {
    int descriptor = openat(scope->directory_fd, name,
                            O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) {
        return 0;
    }
    int result = read_bounded_fd(descriptor, buffer, capacity);
    close(descriptor);
    return result;
}

static int read_self_cgroup_relative(char *relative, size_t capacity) {
    char buffer[PATH_MAX + 32];
    int descriptor = open(SELF_CGROUP_PATH, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) {
        return 0;
    }
    int read_ok = read_bounded_fd(descriptor, buffer, sizeof(buffer));
    close(descriptor);
    if (!read_ok) {
        return 0;
    }

    char *newline = strchr(buffer, '\n');
    if (newline != NULL) {
        if (newline[1] != '\0') {
            return 0;
        }
        *newline = '\0';
    }
    if (strncmp(buffer, "0::/", 4) != 0) {
        return 0;
    }
    const char *value = buffer + 3;
    size_t length = strlen(value);
    if (length == 0 || length >= capacity || value[0] != '/' ||
        strstr(value, "/../") != NULL ||
        (length >= 3U && strcmp(value + length - 3U, "/..") == 0)) {
        return 0;
    }
    memcpy(relative, value, length + 1U);
    return 1;
}

static int cgroup_procs_contains_pid(int root_fd, pid_t pid) {
    char buffer[4096];
    char expected[32];
    int expected_length =
        snprintf(expected, sizeof(expected), "%ld", (long)pid);
    if (expected_length <= 0 ||
        (size_t)expected_length >= sizeof(expected)) {
        return 0;
    }
    int descriptor =
        openat(root_fd, "cgroup.procs", O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) {
        return 0;
    }
    int read_ok = read_bounded_fd(descriptor, buffer, sizeof(buffer));
    close(descriptor);
    if (!read_ok) {
        return 0;
    }
    char *save = NULL;
    for (char *line = strtok_r(buffer, "\n", &save); line != NULL;
         line = strtok_r(NULL, "\n", &save)) {
        if (strcmp(line, expected) == 0) {
            return 1;
        }
    }
    return 0;
}

static int delegated_scope_context(void) {
    const char *nonce = getenv(DELEGATION_NONCE_ENV);
    if (nonce == NULL) {
        delegated_cgroup_relative[0] = '\0';
        return 0;
    }
    if (*nonce == '\0') {
        return -1;
    }
    for (const char *cursor = nonce; *cursor != '\0'; ++cursor) {
        if (*cursor < '0' || *cursor > '9') {
            return -1;
        }
    }
    char own_pid[32];
    int own_pid_length =
        snprintf(own_pid, sizeof(own_pid), "%ld", (long)getpid());
    if (own_pid_length <= 0 || (size_t)own_pid_length >= sizeof(own_pid) ||
        strcmp(nonce, own_pid) != 0) {
        return -1;
    }

    char relative[PATH_MAX + 1];
    if (!read_self_cgroup_relative(relative, sizeof(relative))) {
        return -1;
    }
    const char *component = strrchr(relative, '/');
    if (component == NULL || component[1] == '\0') {
        return -1;
    }
    ++component;

    char expected[128];
    int length = snprintf(expected, sizeof(expected), "%s%s.scope",
                          DELEGATED_SCOPE_PREFIX, nonce);
    if (length <= 0 || (size_t)length >= sizeof(expected) ||
        strcmp(component, expected) != 0) {
        return -1;
    }

    char root_path[PATH_MAX + 1];
    length = snprintf(root_path, sizeof(root_path), "%s%s", CGROUP_ROOT,
                      relative);
    if (length <= 0 || (size_t)length >= sizeof(root_path)) {
        return -1;
    }
    int root_fd =
        open(root_path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    struct statfs filesystem;
    if (root_fd < 0 || fstatfs(root_fd, &filesystem) != 0 ||
        (unsigned long)filesystem.f_type != (unsigned long)CGROUP2_SUPER_MAGIC ||
        !cgroup_procs_contains_pid(root_fd, getpid())) {
        if (root_fd >= 0) {
            close(root_fd);
        }
        return -1;
    }
    close(root_fd);
    memcpy(delegated_cgroup_relative, relative, strlen(relative) + 1U);
    return 1;
}

static int current_cgroup_path(char *path, size_t capacity) {
    char relative[PATH_MAX + 1];
    if (delegated_cgroup_relative[0] == '\0' ||
        !read_self_cgroup_relative(relative, sizeof(relative)) ||
        strcmp(relative, delegated_cgroup_relative) != 0) {
        return 0;
    }
    int length = snprintf(path, capacity, "%s%s/%s", CGROUP_ROOT, relative,
                          CGROUP_PAYLOAD_NAME);
    return length > 0 && (size_t)length < capacity;
}

static int create_installer_cgroup(struct installer_cgroup *scope) {
    char type[64];
    int kill_fd = -1;
    int events_fd = -1;
    int procs_fd = -1;

    scope->directory_fd = -1;
    scope->path[0] = '\0';
    if (!current_cgroup_path(scope->path, sizeof(scope->path)) ||
        mkdir(scope->path, 0700) != 0) {
        return 0;
    }
    scope->directory_fd = open(scope->path,
                               O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (scope->directory_fd < 0 ||
        !read_cgroup_file_at(scope, "cgroup.type", type, sizeof(type)) ||
        (strcmp(type, "domain\n") != 0 && strcmp(type, "domain") != 0)) {
        goto fail;
    }
    kill_fd = openat(scope->directory_fd, "cgroup.kill",
                     O_WRONLY | O_NOFOLLOW | O_CLOEXEC);
    events_fd = openat(scope->directory_fd, "cgroup.events",
                       O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    procs_fd = openat(scope->directory_fd, "cgroup.procs",
                      O_WRONLY | O_NOFOLLOW | O_CLOEXEC);
    if (kill_fd < 0 || events_fd < 0 || procs_fd < 0) {
        goto fail;
    }
    close(kill_fd);
    close(events_fd);
    close(procs_fd);
    return 1;

fail:
    if (kill_fd >= 0) {
        close(kill_fd);
    }
    if (events_fd >= 0) {
        close(events_fd);
    }
    if (procs_fd >= 0) {
        close(procs_fd);
    }
    if (scope->directory_fd >= 0) {
        close(scope->directory_fd);
        scope->directory_fd = -1;
    }
    rmdir(scope->path);
    scope->path[0] = '\0';
    return 0;
}

static int move_child_to_cgroup(const struct installer_cgroup *scope,
                                pid_t child) {
    char pid_text[32];
    int length = snprintf(pid_text, sizeof(pid_text), "%ld\n", (long)child);
    if (length <= 0 || (size_t)length >= sizeof(pid_text)) {
        return 0;
    }
    int descriptor = openat(scope->directory_fd, "cgroup.procs",
                            O_WRONLY | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) {
        return 0;
    }
    ssize_t written;
    do {
        written = write(descriptor, pid_text, (size_t)length);
    } while (written < 0 && errno == EINTR);
    close(descriptor);
    if (written != length) {
        return 0;
    }

    char process_path[64];
    char membership[PATH_MAX + 32];
    length = snprintf(process_path, sizeof(process_path), "/proc/%ld/cgroup",
                      (long)child);
    if (length <= 0 || (size_t)length >= sizeof(process_path)) {
        return 0;
    }
    descriptor = open(process_path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) {
        return 0;
    }
    int read_ok = read_bounded_fd(descriptor, membership, sizeof(membership));
    close(descriptor);
    if (!read_ok) {
        return 0;
    }

    const char *relative = scope->path + strlen(CGROUP_ROOT);
    char expected[PATH_MAX + 8];
    length = snprintf(expected, sizeof(expected), "0::%s", relative);
    if (length <= 0 || (size_t)length >= sizeof(expected)) {
        return 0;
    }
    char *save = NULL;
    for (char *line = strtok_r(membership, "\n", &save); line != NULL;
         line = strtok_r(NULL, "\n", &save)) {
        if (strcmp(line, expected) == 0) {
            return 1;
        }
    }
    return 0;
}

static int write_cgroup_kill(const struct installer_cgroup *scope) {
    int descriptor = openat(scope->directory_fd, "cgroup.kill",
                            O_WRONLY | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) {
        return 0;
    }
    ssize_t written;
    do {
        written = write(descriptor, "1", 1);
    } while (written < 0 && errno == EINTR);
    close(descriptor);
    return written == 1;
}

static int cgroup_is_populated(const struct installer_cgroup *scope) {
    char buffer[256];
    if (!read_cgroup_file_at(scope, "cgroup.events", buffer, sizeof(buffer))) {
        return -1;
    }
    for (char *line = buffer; line != NULL;) {
        char *next = strchr(line, '\n');
        if (next != NULL) {
            *next = '\0';
            ++next;
        }
        if (strcmp(line, "populated 0") == 0) {
            return 0;
        }
        if (strcmp(line, "populated 1") == 0) {
            return 1;
        }
        line = next;
    }
    return -1;
}

static void contain_installer_cgroup(const struct installer_cgroup *scope) {
    struct timespec delay = {0, 10000000L};
    unsigned int stable_empty_checks = 0;

    for (;;) {
        int killed = write_cgroup_kill(scope);
        int populated = cgroup_is_populated(scope);
        if (killed && populated == 0) {
            ++stable_empty_checks;
            if (stable_empty_checks >= 2U) {
                return;
            }
        } else {
            stable_empty_checks = 0;
        }
        struct timespec remaining = delay;
        while (nanosleep(&remaining, &remaining) != 0 && errno == EINTR) {
        }
    }
}

static int remove_installer_cgroup(const struct installer_cgroup *scope) {
    struct stat descriptor_metadata;
    struct stat path_metadata;

    if (fstat(scope->directory_fd, &descriptor_metadata) != 0) {
        return 0;
    }
    if (lstat(scope->path, &path_metadata) != 0) {
        return errno == ENOENT;
    }
    if (!S_ISDIR(path_metadata.st_mode) ||
        descriptor_metadata.st_dev != path_metadata.st_dev ||
        descriptor_metadata.st_ino != path_metadata.st_ino) {
        return 0;
    }
    return rmdir(scope->path) == 0;
}

static void remove_installer_cgroup_or_wait(
    const struct installer_cgroup *scope) {
    struct timespec delay = {0, 10000000L};

    while (!remove_installer_cgroup(scope)) {
        struct timespec remaining = delay;
        while (nanosleep(&remaining, &remaining) != 0 && errno == EINTR) {
        }
    }
}

static void handle_alarm(int signal_number) {
    (void)signal_number;
    if (!kill_pending) {
        timed_out = 1;
        kill_pending = 1;
        signal_child_group(SIGTERM);
        alarm(KILL_GRACE_SECONDS);
        return;
    }
    signal_child_group(SIGKILL);
}

static void handle_forwarded_signal(int signal_number) {
    forwarded_signal = signal_number;
    signal_child_group(signal_number);
    if (!kill_pending) {
        kill_pending = 1;
        alarm(KILL_GRACE_SECONDS);
    }
}

static void managed_signal_set(sigset_t *signals) {
    sigemptyset(signals);
    sigaddset(signals, SIGALRM);
    sigaddset(signals, SIGTERM);
    sigaddset(signals, SIGINT);
    sigaddset(signals, SIGHUP);
    sigaddset(signals, SIGQUIT);
}

static int prepare_signal_state(void) {
    struct sigaction alarm_action = {0};
    struct sigaction default_action = {0};
    struct sigaction forward_action = {0};
    struct sigaction ignore_action = {0};
    sigset_t alarm_only;
    sigset_t managed;
    struct timespec no_wait = {0};

    alarm(0);
    managed_signal_set(&managed);
    if (sigprocmask(SIG_BLOCK, &managed, NULL) != 0) {
        return 0;
    }

    sigfillset(&default_action.sa_mask);
    default_action.sa_handler = SIG_DFL;
    if (sigaction(SIGALRM, &default_action, NULL) != 0 ||
        sigaction(SIGTERM, &default_action, NULL) != 0 ||
        sigaction(SIGINT, &default_action, NULL) != 0 ||
        sigaction(SIGHUP, &default_action, NULL) != 0 ||
        sigaction(SIGQUIT, &default_action, NULL) != 0 ||
        sigaction(SIGCHLD, &default_action, NULL) != 0) {
        return 0;
    }
    sigemptyset(&ignore_action.sa_mask);
    ignore_action.sa_handler = SIG_IGN;
    if (sigaction(SIGPIPE, &ignore_action, NULL) != 0) {
        return 0;
    }

    sigemptyset(&alarm_only);
    sigaddset(&alarm_only, SIGALRM);
    while (sigtimedwait(&alarm_only, NULL, &no_wait) >= 0) {
    }
    if (errno != EAGAIN) {
        return 0;
    }

    sigfillset(&alarm_action.sa_mask);
    alarm_action.sa_handler = handle_alarm;
    sigfillset(&forward_action.sa_mask);
    forward_action.sa_handler = handle_forwarded_signal;

    return sigaction(SIGALRM, &alarm_action, NULL) == 0 &&
           sigaction(SIGTERM, &forward_action, NULL) == 0 &&
           sigaction(SIGINT, &forward_action, NULL) == 0 &&
           sigaction(SIGHUP, &forward_action, NULL) == 0 &&
           sigaction(SIGQUIT, &forward_action, NULL) == 0;
}

static int pending_termination_signal(void) {
    const int signals[] = {SIGTERM, SIGINT, SIGHUP, SIGQUIT};
    sigset_t pending;

    if (sigpending(&pending) != 0) {
        return -1;
    }
    for (size_t index = 0; index < sizeof(signals) / sizeof(signals[0]);
         ++index) {
        if (sigismember(&pending, signals[index]) == 1) {
            return signals[index];
        }
    }
    return 0;
}

static int restore_child_signals(void) {
    struct sigaction action = {0};
    sigemptyset(&action.sa_mask);
    action.sa_handler = SIG_DFL;
    return sigaction(SIGALRM, &action, NULL) == 0 &&
           sigaction(SIGTERM, &action, NULL) == 0 &&
           sigaction(SIGINT, &action, NULL) == 0 &&
           sigaction(SIGHUP, &action, NULL) == 0 &&
           sigaction(SIGQUIT, &action, NULL) == 0 &&
           sigaction(SIGCHLD, &action, NULL) == 0 &&
           sigaction(SIGPIPE, &action, NULL) == 0;
}

static int delegate_to_systemd_scope(void) {
    char nonce[32];
    char unit_argument[160];
    char environment_argument[128];
    int nonce_length = snprintf(nonce, sizeof(nonce), "%ld", (long)getpid());
    if (nonce_length <= 0 || (size_t)nonce_length >= sizeof(nonce)) {
        return 0;
    }
    int unit_length = snprintf(unit_argument, sizeof(unit_argument),
                               "--unit=%s%s.scope", DELEGATED_SCOPE_PREFIX,
                               nonce);
    int environment_length =
        snprintf(environment_argument, sizeof(environment_argument),
                 "--setenv=%s=%s", DELEGATION_NONCE_ENV, nonce);
    if (unit_length <= 0 || (size_t)unit_length >= sizeof(unit_argument) ||
        environment_length <= 0 ||
        (size_t)environment_length >= sizeof(environment_argument)) {
        return 0;
    }
    if (clearenv() != 0 || setenv("PATH", "/usr/bin:/bin", 1) != 0 ||
        !restore_child_signals()) {
        return 0;
    }
    sigset_t clean_mask;
    sigemptyset(&clean_mask);
    if (sigprocmask(SIG_SETMASK, &clean_mask, NULL) != 0) {
        return 0;
    }
    char *const argv[] = {
        (char *)SYSTEMD_RUN_PATH,
        (char *)"--quiet",
        (char *)"--scope",
        (char *)"--collect",
        (char *)"--no-ask-password",
        unit_argument,
        (char *)"--property=Delegate=yes",
        environment_argument,
        (char *)"--",
        (char *)LAUNCHER_PATH,
        NULL,
    };
    execv(SYSTEMD_RUN_PATH, argv);
    return 0;
}

static int wait_for_child_exit(pid_t child) {
    siginfo_t child_info = {0};

    while (waitid(P_PID, (id_t)child, &child_info, WEXITED | WNOWAIT) != 0) {
        if (errno != EINTR) {
            return 0;
        }
    }
    return 1;
}

static int finish_watchdog(int write_fd, pid_t watchdog,
                           const struct installer_cgroup *scope) {
    const char token = 'W';
    ssize_t write_result;
    int status = 0;

    do {
        write_result = write(write_fd, &token, 1);
    } while (write_result < 0 && errno == EINTR);
    close(write_fd);
    while (waitpid(watchdog, &status, 0) < 0) {
        if (errno != EINTR) {
            contain_installer_cgroup(scope);
            remove_installer_cgroup_or_wait(scope);
            return 0;
        }
    }
    int watchdog_ok = WIFEXITED(status) && WEXITSTATUS(status) == 0;
    if (!watchdog_ok) {
        contain_installer_cgroup(scope);
        remove_installer_cgroup_or_wait(scope);
    }
    return write_result == 1 && watchdog_ok;
}

static int abort_installer_after_watchdog(
    int write_fd, pid_t watchdog, pid_t child, int lock_fd,
    const struct installer_cgroup *scope) {
    int status = 0;
    pid_t reaped;

    signal_child_group(SIGKILL);
    int watchdog_ok = finish_watchdog(write_fd, watchdog, scope);
    do {
        reaped = waitpid(child, &status, 0);
    } while (reaped < 0 && errno == EINTR);
    close(scope->directory_fd);
    close(lock_fd);
    (void)status;
    (void)reaped;
    (void)watchdog_ok;
    return fail_preflight();
}

static int abort_installer_before_watchdog(
    int barrier_fd, pid_t child, int lock_fd,
    const struct installer_cgroup *scope) {
    int status;
    pid_t reaped;

    close(barrier_fd);
    signal_child_group(SIGKILL);
    contain_installer_cgroup(scope);
    do {
        reaped = waitpid(child, &status, 0);
    } while (reaped < 0 && errno == EINTR);
    remove_installer_cgroup_or_wait(scope);
    close(scope->directory_fd);
    close(lock_fd);
    (void)status;
    (void)reaped;
    return fail_preflight();
}

static int run_installer(int lock_fd) {
    int barrier[2];
    sigset_t clean_mask;
    if (pipe2(barrier, O_CLOEXEC) != 0) {
        close(lock_fd);
        return fail_preflight();
    }

    pid_t parent = getpid();
    pid_t child = fork();

    if (child < 0) {
        close(barrier[0]);
        close(barrier[1]);
        close(lock_fd);
        return fail_preflight();
    }
    if (child == 0) {
        char token = '\0';
        close(barrier[1]);
        if (prctl(PR_SET_PDEATHSIG, SIGKILL) != 0 || getppid() != parent ||
            setpgid(0, 0) != 0) {
            _exit(127);
        }
        ssize_t read_result;
        do {
            read_result = read(barrier[0], &token, 1);
        } while (read_result < 0 && errno == EINTR);
        close(barrier[0]);
        if (read_result != 1 || token != 'G') {
            _exit(127);
        }
        if (!restore_child_signals()) {
            _exit(127);
        }
        sigemptyset(&clean_mask);
        if (sigprocmask(SIG_SETMASK, &clean_mask, NULL) != 0) {
            _exit(127);
        }
        char *const argv[] = {"/bin/bash", "--noprofile", "--norc",
                              INSTALLER_PATH, NULL};
        execv("/bin/bash", argv);
        _exit(127);
    }

    close(barrier[0]);
    child_process_group = child;
    if (setpgid(child, child) != 0 && errno != EACCES) {
        close(barrier[1]);
        signal_child_group(SIGKILL);
        waitpid(child, NULL, 0);
        close(lock_fd);
        return fail_preflight();
    }

    struct installer_cgroup scope;
    if (!create_installer_cgroup(&scope)) {
        close(barrier[1]);
        signal_child_group(SIGKILL);
        waitpid(child, NULL, 0);
        close(lock_fd);
        return fail_preflight();
    }
    if (!move_child_to_cgroup(&scope, child)) {
        return abort_installer_before_watchdog(barrier[1], child, lock_fd,
                                               &scope);
    }

    int watchdog_pipe[2];
    if (pipe2(watchdog_pipe, O_CLOEXEC) != 0) {
        return abort_installer_before_watchdog(barrier[1], child, lock_fd,
                                               &scope);
    }
    pid_t watchdog = fork();
    if (watchdog < 0) {
        close(watchdog_pipe[0]);
        close(watchdog_pipe[1]);
        return abort_installer_before_watchdog(barrier[1], child, lock_fd,
                                               &scope);
    }
    if (watchdog == 0) {
        char token = '\0';
        close(watchdog_pipe[1]);
        close(barrier[1]);
        ssize_t read_result;
        do {
            read_result = read(watchdog_pipe[0], &token, 1);
        } while (read_result < 0 && errno == EINTR);
        close(watchdog_pipe[0]);
        (void)token;
        contain_installer_cgroup(&scope);
        remove_installer_cgroup_or_wait(&scope);
        close(scope.directory_fd);
        _exit(0);
    }
    close(watchdog_pipe[0]);
    sigemptyset(&clean_mask);
    alarm(INSTALL_TIMEOUT_SECONDS);
    if (sigprocmask(SIG_SETMASK, &clean_mask, NULL) != 0) {
        alarm(0);
        close(barrier[1]);
        return abort_installer_after_watchdog(watchdog_pipe[1], watchdog,
                                               child, lock_fd, &scope);
    }
    if (kill_pending) {
        close(barrier[1]);
    } else {
        const char token = 'G';
        if (write(barrier[1], &token, 1) != 1) {
            alarm(0);
            close(barrier[1]);
            return abort_installer_after_watchdog(watchdog_pipe[1], watchdog,
                                                   child, lock_fd, &scope);
        }
        close(barrier[1]);
    }

    if (!wait_for_child_exit(child)) {
        alarm(0);
        return abort_installer_after_watchdog(watchdog_pipe[1], watchdog,
                                               child, lock_fd, &scope);
    }
    signal_child_group(SIGKILL);
    alarm(0);
    int watchdog_ok = finish_watchdog(watchdog_pipe[1], watchdog, &scope);
    int status = 0;
    pid_t reaped;
    do {
        reaped = waitpid(child, &status, 0);
    } while (reaped < 0 && errno == EINTR);
    close(scope.directory_fd);
    close(lock_fd);
    if (reaped != child || !watchdog_ok) {
        return fail_preflight();
    }

    if (WIFEXITED(status) && WEXITSTATUS(status) == 2) {
        return 2;
    }
    if (timed_out) {
        return emit_launcher_event("launcher_timeout", 0, 124);
    }
    if (forwarded_signal) {
        return 128 + forwarded_signal;
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return fail_preflight();
}

int main(int argc, char *argv[]) {
    (void)argv;
    if (argc != 1) {
        return fail_preflight();
    }
    umask(0077);
    if (!prepare_signal_state()) {
        return fail_preflight();
    }
    int pending_signal = pending_termination_signal();
    if (pending_signal < 0) {
        return fail_preflight();
    }
    if (pending_signal > 0) {
        return emit_launcher_event("launcher_cancelled", 0,
                                   128 + pending_signal);
    }
    if (geteuid() != REQUIRED_UID || !running_from_approved_inode() ||
        !secure_path(STATE_DIR, 0700, 1) ||
        !secure_path(LAUNCHER_PATH, 0700, 0) ||
        !secure_path(INSTALLER_PATH, 0700, 0) ||
        !secure_path(SHA256SUM_PATH, 0755, 0) ||
        !secure_path(SYSTEMD_RUN_PATH, 0755, 0)) {
        return fail_preflight();
    }

    close_extra_fds();
    int delegation = delegated_scope_context();
    if (delegation < 0) {
        return fail_preflight();
    }
    if (delegation == 0) {
        if (!delegate_to_systemd_scope()) {
            return fail_preflight();
        }
        return fail_preflight();
    }
    if (clearenv() != 0 || setenv("PATH", "/usr/bin:/bin", 1) != 0 ||
        setenv("CUSTOMS_CRON_INSTALL_CLEAN", "1", 1) != 0) {
        return fail_preflight();
    }
    if (!verify_installer_digest()) {
        return fail_preflight();
    }
    pending_signal = pending_termination_signal();
    if (pending_signal < 0) {
        return fail_preflight();
    }
    if (pending_signal > 0) {
        return emit_launcher_event("launcher_cancelled", 0,
                                   128 + pending_signal);
    }
    int lock_fd = acquire_install_lock();
    if (lock_fd == -2) {
        return emit_launcher_event("skipped_locked", 0, 75);
    }
    if (lock_fd < 0) {
        return emit_launcher_event("lock_failed", 0, 74);
    }
    pending_signal = pending_termination_signal();
    if (pending_signal < 0) {
        close(lock_fd);
        return fail_preflight();
    }
    if (pending_signal > 0) {
        close(lock_fd);
        return emit_launcher_event("launcher_cancelled", 0,
                                   128 + pending_signal);
    }
    return run_installer(lock_fd);
}
