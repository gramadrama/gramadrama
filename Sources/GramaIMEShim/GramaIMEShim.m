#import "GramaIMEShim.h"

NSString *GDTry(void (NS_NOESCAPE ^block)(void)) {
    @try {
        block();
        return nil;
    } @catch (NSException *exception) {
        return exception.name ?: @"NSException";
    }
}
