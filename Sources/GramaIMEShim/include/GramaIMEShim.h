#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

/// Wykonuje blok i zwraca nazwę wyjątku Objective-C, jeśli blok go rzucił (nil — bez wyjątku).
/// Wywołania IMK na kliencie, którego proces już nie istnieje, rzucają NSException, a Swift jej nie złapie:
/// bez tego każda taka próba kończyłaby proces metody wprowadzania (a macOS liczy wyjścia i po serii blokuje IME).
/// Zwraca samą nazwę wyjątku — opis mógłby zawierać tekst z pola.
NSString * _Nullable GDTry(void (NS_NOESCAPE ^block)(void));

NS_ASSUME_NONNULL_END
