@interface Worker : NSObject
- (void)run;
@end
@implementation Worker
- (void)run { [self helper]; }
- (void)helper {}
@end
