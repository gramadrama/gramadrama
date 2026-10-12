import Foundation
import GramaCore

/// Wspólna pamięć odpowiedzi serwera: ten sam tekst w tym samym trybie liczymy raz.
/// - Wynik jest pamiętany (ostatnie `capacity` zapytań, tylko w pamięci procesu — nigdy na dysku).
/// - Takie samo zapytanie w toku: dołączamy do niego, zamiast wysyłać drugie (⌥⌘G w trakcie sprawdzania
///   tego samego tekstu w tle, dwa szybkie przełączenia trybu).
/// - Zapytanie, na które nikt już nie czeka, przerywamy po `grace` — do tego czasu panel może do niego
///   dołączyć (⌥⌘G przerywa sprawdzanie w tle, zanim pobierze tekst). Nowe zapytanie z panelu przerywa
///   takie osierocone zapytania od razu, żeby serwer obsłużył panel.
@MainActor
package final class RequestCache<Value> {
    package enum Source: String { case model, cache, joined }

    private final class Flight {
        let key: String
        var task: Task<Void, Never>?
        var waiters: [UUID: CheckedContinuation<Value, Error>] = [:]
        var subscribers: [UUID: @MainActor (String) -> Void] = [:]
        var latestPartial = ""
        var orphaned = false

        init(key: String) { self.key = key }
    }

    let name: String
    private let capacity: Int
    private let grace: TimeInterval
    private var results: [String: Value] = [:]
    private var order: [String] = []
    private var flights: [String: Flight] = [:]
    /// Liczniki do testów i do pomiaru (etap 4C).
    package private(set) var computed = 0

    package init(name: String, capacity: Int = 64, grace: TimeInterval = 1.5) {
        self.name = name
        self.capacity = capacity
        self.grace = grace
    }

    /// `compute` dostaje funkcję, którą przekazuje częściowe wyniki (strumień) wszystkim czekającym.
    /// `log`: zapis do logu zdarzeń (bez treści — tylko rodzaj, źródło i długość).
    /// `onWait`: wołane od razu, gdy wyniku nie ma w pamięci i trzeba poczekać na serwer (wskaźnik „sprawdzam”).
    package func value(for key: String, chars: Int, background: Bool = false, log: Bool = true,
               onPartial: @escaping @MainActor (String) -> Void = { _ in },
               onWait: @MainActor () -> Void = {},
               compute: @escaping (@escaping @MainActor (String) -> Void) async throws -> Value) async throws -> (Value, Source) {
        func record(_ source: Source) {
            guard log else { return }
            EventLog.write("request", ["kind": name, "source": source.rawValue, "background": background, "chars": chars])
        }
        if let hit = results[key] {
            touch(key)
            record(.cache)
            return (hit, .cache)
        }
        onWait()
        let source: Source
        let flight: Flight
        if let existing = flights[key] {
            flight = existing
            source = .joined
        } else {
            if !background { cancelOrphans() }
            flight = Flight(key: key)
            flights[key] = flight
            computed += 1
            source = .model
            flight.task = Task { [weak self] in
                let result: Result<Value, Error>
                do {
                    result = .success(try await compute { [weak flight] partial in
                        guard let flight else { return }
                        flight.latestPartial = partial
                        for subscriber in flight.subscribers.values { subscriber(partial) }
                    })
                } catch {
                    result = .failure(error)
                }
                self?.complete(flight, result)
            }
        }
        record(source)
        let id = UUID()
        let value = try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Value, Error>) in
                guard !Task.isCancelled else {
                    continuation.resume(throwing: CancellationError())
                    orphanIfIdle(flight)
                    return
                }
                flight.waiters[id] = continuation
                flight.subscribers[id] = onPartial
                flight.orphaned = false
                if source == .joined, !flight.latestPartial.isEmpty { onPartial(flight.latestPartial) }
            }
        } onCancel: {
            Task { @MainActor [weak self] in self?.cancelWaiter(id, in: flight) }
        }
        return (value, source)
    }

    /// Zapomina wyniki (np. po zmianie modelu w ustawieniach klucz i tak się zmienia — to na wszelki wypadek).
    func removeAll() {
        results = [:]
        order = []
    }

    private func complete(_ flight: Flight, _ result: Result<Value, Error>) {
        if flights[flight.key] === flight { flights[flight.key] = nil }
        if case .success(let value) = result {
            results[flight.key] = value
            touch(flight.key)
        }
        let waiters = flight.waiters
        flight.waiters = [:]
        flight.subscribers = [:]
        for continuation in waiters.values { continuation.resume(with: result) }
    }

    private func cancelWaiter(_ id: UUID, in flight: Flight) {
        flight.subscribers[id] = nil
        guard let continuation = flight.waiters.removeValue(forKey: id) else { return }
        continuation.resume(throwing: CancellationError())
        orphanIfIdle(flight)
    }

    private func orphanIfIdle(_ flight: Flight) {
        guard flight.waiters.isEmpty, flights[flight.key] === flight, !flight.orphaned else { return }
        flight.orphaned = true
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64((self?.grace ?? 0) * 1_000_000_000))
            guard let self, flight.orphaned, flight.waiters.isEmpty else { return }
            self.drop(flight)
        }
    }

    private func cancelOrphans() {
        for flight in flights.values where flight.orphaned && flight.waiters.isEmpty { drop(flight) }
    }

    private func drop(_ flight: Flight) {
        flight.task?.cancel()
        if flights[flight.key] === flight { flights[flight.key] = nil }
    }

    private func touch(_ key: String) {
        order.removeAll { $0 == key }
        order.append(key)
        while order.count > capacity { results[order.removeFirst()] = nil }
    }
}
